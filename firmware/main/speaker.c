#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_http_server.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "speaker.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"

#define FRAME_SAMPLES 960
#define QUEUE_FRAMES 32
typedef struct __attribute__((packed)) {
    char magic[4];
    uint32_t epoch, sequence;
    uint16_t samples, flags;
    int16_t pcm[FRAME_SAMPLES];
} speaker_packet_t;
_Static_assert(sizeof(speaker_packet_t) == 16 + FRAME_SAMPLES * 2, "Speaker packet layout");
typedef struct {int fd; uint32_t connection; char text[192];} notice_t;

static QueueHandle_t frames;
static esp_codec_dev_handle_t codec_dev;
static i2s_chan_handle_t tx_channel;
static httpd_handle_t server;
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static volatile bool ready, active, amplifier, ended;
static volatile int owner = -1, volume = 75;
static volatile uint32_t connection, epoch = 1, received, consumed, played_total, stale, rejected, errors, cancels;
static int64_t last_frame_us;

bool speaker_enabled(void) {return amplifier;}

void speaker_cancel(void)
{
    portENTER_CRITICAL(&lock);
    epoch++; if (!epoch) epoch++;
    active = ended = amplifier = false;
    cancels++;
    gpio_set_level(GPIO_NUM_53, 0);
    portEXIT_CRITICAL(&lock);
    if (frames) xQueueReset(frames);
}

static void send_notice_work(void *argument)
{
    notice_t *notice = argument;
    if (owner == notice->fd && connection == notice->connection) {
        httpd_ws_frame_t reply = {.type = HTTPD_WS_TYPE_TEXT,
            .payload = (uint8_t *)notice->text, .len = strlen(notice->text)};
        if (httpd_ws_send_frame_async(server, notice->fd, &reply) != ESP_OK) {
            speaker_cancel();
            httpd_sess_trigger_close(server, notice->fd);
        }
    }
    free(notice);
}

static void notify(const char *type, uint32_t turn, uint32_t count)
{
    if (owner < 0 || !server) return;
    notice_t *notice = calloc(1, sizeof(*notice));
    if (!notice) {speaker_cancel(); return;}
    notice->fd = owner; notice->connection = connection;
    snprintf(notice->text, sizeof(notice->text),
        "{\"type\":\"%s\",\"epoch\":%lu,\"consumed\":%lu,\"window\":16}",
        type, (unsigned long)turn, (unsigned long)count);
    if (httpd_queue_work(server, send_notice_work, notice) != ESP_OK) {
        free(notice); speaker_cancel();
    }
}

static void playback_task(void *argument)
{
    speaker_packet_t packet;
    int16_t stereo[FRAME_SAMPLES * 2];
    uint32_t previous_epoch = 0;
    for (;;) {
        if (xQueueReceive(frames, &packet, pdMS_TO_TICKS(30)) == pdPASS) {
            if (!active || packet.epoch != epoch) continue;
            if (previous_epoch != packet.epoch) {
                // Let old I2S DMA drain while muted before enabling a new turn.
                vTaskDelay(pdMS_TO_TICKS(50));
                previous_epoch = packet.epoch;
            }
            if (!active || packet.epoch != epoch) continue;
            for (int n = 0; n < packet.samples; n++) stereo[n*2] = stereo[n*2+1] = packet.pcm[n];
            portENTER_CRITICAL(&lock);
            if (active && packet.epoch == epoch) {
                amplifier = volume > 0;
                gpio_set_level(GPIO_NUM_53, amplifier);
            }
            portEXIT_CRITICAL(&lock);
            if (!active || packet.epoch != epoch) continue;
            size_t written = 0, length = packet.samples * 4;
            esp_err_t ret = i2s_channel_write(tx_channel, stereo, length, &written, 100);
            if (ret != ESP_OK || written != length) {
                errors++; speaker_cancel(); notify("play_error", packet.epoch, consumed); continue;
            }
            if (packet.epoch == epoch && active) {
                consumed++; played_total++;
                if (consumed % 4 == 0) notify("consumed", epoch, consumed);
            }
        }
        if (active && ended && uxQueueMessagesWaiting(frames) == 0 && consumed == received) {
            uint32_t completed_epoch = epoch;
            vTaskDelay(pdMS_TO_TICKS(50)); // Physical DMA tail, then mute.
            portENTER_CRITICAL(&lock);
            bool completed = active && epoch == completed_epoch && consumed == received;
            if (completed) {
                amplifier = active = false;
                gpio_set_level(GPIO_NUM_53, 0);
            }
            portEXIT_CRITICAL(&lock);
            if (completed) notify("drained", completed_epoch, consumed);
        } else if (active && !ended && esp_timer_get_time() - last_frame_us > 3000000) {
            uint32_t abandoned_epoch = epoch;
            speaker_cancel(); notify("stream_timeout", abandoned_epoch, consumed);
        }
    }
}

static esp_err_t reply(httpd_req_t *request, const char *type)
{
    char text[192];
    snprintf(text, sizeof(text),
        "{\"type\":\"%s\",\"epoch\":%lu,\"consumed\":%lu,\"window\":16,\"sample_rate\":48000}",
        type, (unsigned long)epoch, (unsigned long)consumed);
    httpd_ws_frame_t frame = {.type = HTTPD_WS_TYPE_TEXT, .payload = (uint8_t *)text, .len = strlen(text)};
    return httpd_ws_send_frame(request, &frame);
}

static esp_err_t reject_stream(httpd_req_t *request, const char *type)
{
    rejected++;
    speaker_cancel();
    return reply(request, type);
}

static esp_err_t speaker_handler(httpd_req_t *request)
{
    int fd = httpd_req_to_sockfd(request);
    if (request->method == HTTP_GET) {
        if (!ready || owner >= 0) {reply(request, "speaker_busy"); return ESP_FAIL;}
        int no_delay = 1;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &no_delay, sizeof(no_delay));
        owner = fd; connection++;
        return reply(request, "hello");
    }
    if (fd != owner) return ESP_FAIL;
    httpd_ws_frame_t frame = {0};
    ESP_RETURN_ON_ERROR(httpd_ws_recv_frame(request, &frame, 0), "speaker", "Frame header");
    if (!frame.len || frame.len > sizeof(speaker_packet_t)) {rejected++; return ESP_FAIL;}
    uint8_t payload[sizeof(speaker_packet_t) + 1];
    frame.payload = payload;
    ESP_RETURN_ON_ERROR(httpd_ws_recv_frame(request, &frame, frame.len), "speaker", "Frame payload");
    if (frame.type == HTTPD_WS_TYPE_TEXT) {
        payload[frame.len] = 0;
        cJSON *json = cJSON_Parse((char *)payload);
        const char *type = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(json, "type"));
        if (!type) {cJSON_Delete(json); return reject_stream(request, "invalid_control");}
        if (!strcmp(type, "begin")) {
            cJSON *requested_volume = cJSON_GetObjectItemCaseSensitive(json, "volume");
            if (!cJSON_IsNumber(requested_volume) || !isfinite(requested_volume->valuedouble) ||
                requested_volume->valuedouble < 0 || requested_volume->valuedouble > 100) {
                cJSON_Delete(json); return reject_stream(request, "invalid_volume");
            }
            speaker_cancel();
            int next_volume = requested_volume->valueint;
            if (esp_codec_dev_set_out_vol(codec_dev, next_volume) != 0) {
                cJSON_Delete(json); errors++; return reply(request, "codec_error");
            }
            portENTER_CRITICAL(&lock);
            volume = next_volume; received = consumed = 0;
            active = true; ended = false; last_frame_us = esp_timer_get_time();
            portEXIT_CRITICAL(&lock);
            cJSON_Delete(json); return reply(request, "begun");
        }
        if (!strcmp(type, "cancel")) {
            cJSON_Delete(json); speaker_cancel(); return reply(request, "cancelled");
        }
        cJSON *turn = cJSON_GetObjectItemCaseSensitive(json, "epoch");
        if (!strcmp(type, "end") && cJSON_IsNumber(turn)) {
            if (turn->valuedouble != epoch || !active) {
                cJSON_Delete(json); stale++; return reply(request, "stale");
            }
            ended = true; cJSON_Delete(json); return reply(request, "ended");
        }
        cJSON_Delete(json); return reject_stream(request, "invalid_control");
    }
    if (frame.type != HTTPD_WS_TYPE_BINARY || frame.len < 18) return reject_stream(request, "invalid_frame");
    speaker_packet_t packet = {0};
    memcpy(&packet, payload, frame.len);
    if (memcmp(packet.magic, "BLP1", 4) || !packet.samples || packet.samples > FRAME_SAMPLES ||
        frame.len != 16 + packet.samples * 2 || packet.flags) {
        return reject_stream(request, "invalid_frame");
    }
    if (!active || ended || packet.epoch != epoch) {stale++; return reply(request, "stale");}
    if (packet.sequence < received) return reply(request, "duplicate");
    if (packet.sequence != received) return reject_stream(request, "sequence_gap");
    if (xQueueSend(frames, &packet, 0) != pdPASS) {rejected++; speaker_cancel(); return reply(request, "overflow");}
    received++; last_frame_us = esp_timer_get_time();
    return ESP_OK;
}

static void close_session(httpd_handle_t handle, int fd)
{
    if (owner == fd) {speaker_cancel(); owner = -1; connection++;}
    close(fd);
}

esp_err_t speaker_init(esp_codec_dev_handle_t codec, i2s_chan_handle_t tx)
{
    codec_dev = codec; tx_channel = tx;
    frames = xQueueCreateWithCaps(QUEUE_FRAMES, sizeof(speaker_packet_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    ESP_RETURN_ON_FALSE(frames, ESP_ERR_NO_MEM, "speaker", "Playback queue");
    ESP_RETURN_ON_FALSE(esp_codec_dev_set_out_vol(codec_dev, 75) == 0, ESP_FAIL, "speaker", "DAC volume");
    ESP_RETURN_ON_FALSE(xTaskCreate(playback_task, "speaker_play", 8192, NULL, 7, NULL) == pdPASS,
                        ESP_ERR_NO_MEM, "speaker", "Playback task");
    ready = true;
    return ESP_OK;
}

esp_err_t speaker_server_start(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 82; config.ctrl_port = 32770;
    config.stack_size = 8192; config.max_open_sockets = 2;
    config.send_wait_timeout = 2; config.recv_wait_timeout = 2;
    config.close_fn = close_session;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &config), "speaker", "WebSocket server");
    const httpd_uri_t route = {.uri = "/speaker", .method = HTTP_GET,
        .handler = speaker_handler, .is_websocket = true};
    return httpd_register_uri_handler(server, &route);
}

cJSON *speaker_status(void)
{
    cJSON *json = cJSON_CreateObject();
    cJSON_AddBoolToObject(json, "ready", ready);
    cJSON_AddBoolToObject(json, "connected", owner >= 0);
    cJSON_AddBoolToObject(json, "active", active);
    cJSON_AddBoolToObject(json, "amplifier_enabled", amplifier);
    cJSON_AddNumberToObject(json, "sample_rate", 48000);
    cJSON_AddNumberToObject(json, "volume", volume);
    cJSON_AddNumberToObject(json, "epoch", epoch);
    cJSON_AddNumberToObject(json, "received_frames", received);
    cJSON_AddNumberToObject(json, "consumed_frames", consumed);
    cJSON_AddNumberToObject(json, "total_consumed_frames", played_total);
    cJSON_AddNumberToObject(json, "stale_frames", stale);
    cJSON_AddNumberToObject(json, "rejected_frames", rejected);
    cJSON_AddNumberToObject(json, "write_errors", errors);
    cJSON_AddNumberToObject(json, "queued_frames", frames ? uxQueueMessagesWaiting(frames) : 0);
    cJSON_AddNumberToObject(json, "cancel_count", cancels);
    return json;
}
