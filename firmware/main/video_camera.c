/* Camera setup follows the project's verified Espressif V4L2 example.
 * One shared I2C bus controls OV5647 and ES8311; video never holds a lock
 * while waiting for the WLAN. Only the newest JPEG is sent to the PC. */
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_video_device.h"
#include "esp_video_init.h"
#include "esp_video_ioctl.h"
#include "driver/jpeg_encode.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "video_camera.h"

static const char *TAG = "camera";
#define BUFFER_COUNT 3
static int video_fd = -1;
static uint8_t *buffers[BUFFER_COUNT], *jpeg_output, *latest_jpeg;
static uint32_t input_size, width, height, jpeg_capacity, latest_size;
static jpeg_encoder_handle_t encoder;
static jpeg_encode_cfg_t encoding;
static SemaphoreHandle_t frame_lock;
static volatile bool ready, streaming, snapshot_requested;
static volatile int quality = 40;
static volatile uint32_t encoded_frames, encode_errors, capture_errors;
static int64_t frame_time_us;

bool video_camera_ready(void) {return ready;}

static void capture_task(void *argument)
{
    int64_t next_encode = 0;
    for (;;) {
        struct v4l2_buffer frame = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE, .memory = V4L2_MEMORY_MMAP};
        if (ioctl(video_fd, VIDIOC_DQBUF, &frame) != 0) {
            capture_errors++; vTaskDelay(pdMS_TO_TICKS(50)); continue;
        }
        int64_t now = esp_timer_get_time();
        if ((frame.flags & V4L2_BUF_FLAG_DONE) && frame.index < BUFFER_COUNT &&
                (streaming || snapshot_requested) && now >= next_encode) {
            next_encode = now + 50000; // At most 20 JPEG/s, independent of sensor's 50 fps.
            jpeg_encode_cfg_t config = encoding;
            config.image_quality = quality;
            uint32_t length = 0;
            esp_err_t result = jpeg_encoder_process(encoder, &config, buffers[frame.index],
                input_size, jpeg_output, jpeg_capacity, &length);
            if (result == ESP_OK && length && length <= jpeg_capacity) {
                if (xSemaphoreTake(frame_lock, pdMS_TO_TICKS(20)) == pdPASS) {
                    memcpy(latest_jpeg, jpeg_output, length);
                    latest_size = length; frame_time_us = esp_timer_get_time(); encoded_frames++;
                    snapshot_requested = false;
                    xSemaphoreGive(frame_lock);
                }
            } else encode_errors++;
        }
        if (ioctl(video_fd, VIDIOC_QBUF, &frame) != 0) capture_errors++;
    }
}

esp_err_t video_camera_init(i2c_master_bus_handle_t bus)
{
    ESP_RETURN_ON_FALSE(bus, ESP_ERR_INVALID_STATE, TAG, "Shared I2C bus is unavailable");
    esp_video_init_csi_config_t csi = {.sccb_config = {.init_sccb = false,
        .i2c_handle = bus, .freq = 100000}, .reset_pin = -1, .pwdn_pin = -1};
    esp_video_init_config_t config = {.csi = &csi};
    ESP_RETURN_ON_ERROR(esp_video_init_with_flags(&config,
        ESP_VIDEO_INIT_FLAGS_MIPI_CSI | ESP_VIDEO_INIT_FLAGS_ISP), TAG, "Video init");
    video_fd = open(ESP_VIDEO_MIPI_CSI_DEVICE_NAME, O_RDONLY);
    ESP_RETURN_ON_FALSE(video_fd >= 0, ESP_FAIL, TAG, "Open CSI device");
    struct v4l2_format format = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE};
    ESP_RETURN_ON_FALSE(ioctl(video_fd, VIDIOC_G_FMT, &format) == 0, ESP_FAIL, TAG, "Read format");
    if (format.fmt.pix.pixelformat == V4L2_PIX_FMT_RGB565X) {
        format.fmt.pix.pixelformat = V4L2_PIX_FMT_RGB565;
        ESP_RETURN_ON_FALSE(ioctl(video_fd, VIDIOC_S_FMT, &format) == 0, ESP_FAIL, TAG, "RGB565 little endian");
    }
    width = format.fmt.pix.width; height = format.fmt.pix.height;
    ESP_RETURN_ON_FALSE(format.fmt.pix.pixelformat == V4L2_PIX_FMT_RGB565,
        ESP_ERR_NOT_SUPPORTED, TAG, "Expected ISP RGB565 output");
    struct v4l2_requestbuffers request = {.count = BUFFER_COUNT, .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
        .memory = V4L2_MEMORY_MMAP};
    ESP_RETURN_ON_FALSE(ioctl(video_fd, VIDIOC_REQBUFS, &request) == 0 && request.count >= BUFFER_COUNT,
        ESP_ERR_NO_MEM, TAG, "Request video buffers");
    for (int n = 0; n < BUFFER_COUNT; n++) {
        struct v4l2_buffer frame = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
            .memory = V4L2_MEMORY_MMAP, .index = n};
        ESP_RETURN_ON_FALSE(ioctl(video_fd, VIDIOC_QUERYBUF, &frame) == 0, ESP_FAIL, TAG, "Query video buffer");
        buffers[n] = mmap(NULL, frame.length, PROT_READ | PROT_WRITE, MAP_SHARED, video_fd, frame.m.offset);
        ESP_RETURN_ON_FALSE(buffers[n] != MAP_FAILED, ESP_ERR_NO_MEM, TAG, "Map video buffer");
        input_size = frame.length;
        ESP_RETURN_ON_FALSE(ioctl(video_fd, VIDIOC_QBUF, &frame) == 0, ESP_FAIL, TAG, "Queue video buffer");
    }
    encoding = (jpeg_encode_cfg_t){.width = width, .height = height,
        .src_type = JPEG_ENCODE_IN_FORMAT_RGB565, .sub_sample = JPEG_DOWN_SAMPLING_YUV422,
        .image_quality = quality};
    jpeg_encode_engine_cfg_t engine_config = {.timeout_ms = 200};
    ESP_RETURN_ON_ERROR(jpeg_new_encoder_engine(&engine_config, &encoder), TAG, "JPEG encoder");
    size_t allocated = 0;
    jpeg_encode_memory_alloc_cfg_t memory_config = {.buffer_direction = JPEG_ENC_ALLOC_OUTPUT_BUFFER};
    jpeg_output = jpeg_alloc_encoder_mem(width * height * 2, &memory_config, &allocated);
    ESP_RETURN_ON_FALSE(jpeg_output && allocated <= UINT32_MAX, ESP_ERR_NO_MEM, TAG, "JPEG output buffer");
    jpeg_capacity = allocated;
    latest_jpeg = heap_caps_malloc(jpeg_capacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    frame_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(latest_jpeg && frame_lock, ESP_ERR_NO_MEM, TAG, "JPEG frame cache");
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    ESP_RETURN_ON_FALSE(ioctl(video_fd, VIDIOC_STREAMON, &type) == 0, ESP_FAIL, TAG, "Start sensor");
    ESP_RETURN_ON_FALSE(xTaskCreate(capture_task, "camera_capture", 6144, NULL, 5, NULL) == pdPASS,
        ESP_ERR_NO_MEM, TAG, "Camera capture task");
    ready = true;
    ESP_LOGI(TAG, "OV5647 and ES8311 share I2C; JPEG %lux%lu, preview max 20 fps", (unsigned long)width, (unsigned long)height);
    return ESP_OK;
}

static bool copy_frame(uint8_t *destination, uint32_t *length, uint32_t *sequence)
{
    bool available = false;
    if (xSemaphoreTake(frame_lock, pdMS_TO_TICKS(50)) == pdPASS) {
        if (latest_size && esp_timer_get_time()-frame_time_us < 2000000 &&
                (!sequence || *sequence != encoded_frames)) {
            memcpy(destination, latest_jpeg, latest_size); *length = latest_size;
            if (sequence) *sequence = encoded_frames;
            available = true;
        }
        xSemaphoreGive(frame_lock);
    }
    return available;
}

static esp_err_t stream_handler(httpd_req_t *request)
{
    if (!ready || streaming) {httpd_resp_set_status(request, "503 Service Unavailable"); return httpd_resp_sendstr(request, "Camera unavailable");}
    uint8_t *packet = heap_caps_malloc(jpeg_capacity + 256, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!packet) return ESP_ERR_NO_MEM;
    int no_delay = 1;
    setsockopt(httpd_req_to_sockfd(request), IPPROTO_TCP, TCP_NODELAY, &no_delay, sizeof(no_delay));
    httpd_resp_set_type(request, "multipart/x-mixed-replace;boundary=benchlink");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    streaming = true;
    uint32_t sequence = 0;
    int64_t last_sent = esp_timer_get_time();
    esp_err_t result = ESP_OK;
    while (result == ESP_OK) {
        uint32_t length = 0;
        // Reserve a small prefix and coalesce metadata + image into one chunk.
        if (copy_frame(packet + 192, &length, &sequence)) {
            char prefix[192];
            int count = snprintf(prefix, sizeof(prefix), "--benchlink\r\nContent-Type: image/jpeg\r\nContent-Length: %lu\r\n\r\n", (unsigned long)length);
            memcpy(packet + 192 - count, prefix, count);
            memcpy(packet + 192 + length, "\r\n", 2);
            result = httpd_resp_send_chunk(request, (char *)packet + 192 - count, count + length + 2);
            last_sent = esp_timer_get_time();
        } else {
            if (esp_timer_get_time() - last_sent > 5000000) {result = ESP_ERR_TIMEOUT; break;}
            vTaskDelay(pdMS_TO_TICKS(5));
        }
    }
    streaming = false;
    free(packet);
    return result;
}

static esp_err_t camera_info_handler(httpd_req_t *request)
{
    char json[512];
    snprintf(json, sizeof(json), "{\"cameras\":[{\"index\":0,\"src\":\":83/stream\",\"currentFrameRate\":20,\"currentQuality\":%d,\"currentImageFormat\":0,\"currentResolution\":{\"width\":%lu,\"height\":%lu}}]}", quality, (unsigned long)width, (unsigned long)height);
    httpd_resp_set_type(request, "application/json");
    return httpd_resp_sendstr(request, ready ? json : "{\"cameras\":[]}");
}

static esp_err_t camera_config_handler(httpd_req_t *request)
{
    if (!request->content_len || request->content_len > 512) return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Invalid config size");
    char text[513] = {0}; int received = 0;
    while (received < request->content_len) {
        int count = httpd_req_recv(request, text + received, request->content_len - received);
        if (count <= 0) return ESP_FAIL;
        received += count;
    }
    cJSON *json = cJSON_Parse(text), *value = cJSON_GetObjectItemCaseSensitive(json, "jpeg_quality");
    bool valid = ready && cJSON_IsNumber(value) && isfinite(value->valuedouble) &&
        value->valuedouble == value->valueint && value->valueint >= 20 && value->valueint <= 90;
    if (valid) quality = value->valueint;
    cJSON_Delete(json);
    if (!valid) return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "JPEG quality must be 20..90");
    return httpd_resp_sendstr(request, "OK");
}

static esp_err_t camera_capture_handler(httpd_req_t *request)
{
    if (!ready) {httpd_resp_set_status(request, "503 Service Unavailable"); return httpd_resp_sendstr(request, "Camera unavailable");}
    uint8_t *copy = heap_caps_malloc(jpeg_capacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!copy) return ESP_ERR_NO_MEM;
    snapshot_requested = true;
    uint32_t length = 0;
    int64_t deadline = esp_timer_get_time() + 2500000;
    while (!copy_frame(copy, &length, NULL) && esp_timer_get_time() < deadline) vTaskDelay(pdMS_TO_TICKS(20));
    esp_err_t result;
    if (length) {httpd_resp_set_type(request, "image/jpeg"); result = httpd_resp_send(request, (char *)copy, length);}
    else {httpd_resp_set_status(request, "503 Service Unavailable"); result = httpd_resp_sendstr(request, "No fresh camera frame");}
    free(copy);
    return result;
}

esp_err_t video_camera_register(httpd_handle_t server)
{
    const httpd_uri_t routes[] = {
        {.uri = "/api/get_camera_info", .method = HTTP_GET, .handler = camera_info_handler},
        {.uri = "/api/set_camera_config", .method = HTTP_POST, .handler = camera_config_handler},
        {.uri = "/api/capture_image", .method = HTTP_GET, .handler = camera_capture_handler}};
    for (int n = 0; n < sizeof(routes)/sizeof(routes[0]); n++)
        ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &routes[n]), TAG, "Camera control route");
    return ESP_OK;
}

esp_err_t video_camera_server_start(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 83; config.ctrl_port = 32771; config.stack_size = 6144;
    config.max_open_sockets = 2; config.send_wait_timeout = 3;
    httpd_handle_t server;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &config), TAG, "Camera stream server");
    httpd_uri_t route = {.uri = "/stream", .method = HTTP_GET, .handler = stream_handler};
    return httpd_register_uri_handler(server, &route);
}

cJSON *video_camera_status(void)
{
    cJSON *json = cJSON_CreateObject();
    cJSON_AddBoolToObject(json, "ready", ready);
    cJSON_AddBoolToObject(json, "streaming", streaming);
    cJSON_AddNumberToObject(json, "width", width); cJSON_AddNumberToObject(json, "height", height);
    cJSON_AddNumberToObject(json, "quality", quality);
    cJSON_AddNumberToObject(json, "max_preview_fps", 20);
    cJSON_AddNumberToObject(json, "encoded_frames", encoded_frames);
    cJSON_AddNumberToObject(json, "capture_errors", capture_errors);
    cJSON_AddNumberToObject(json, "encode_errors", encode_errors);
    return json;
}
