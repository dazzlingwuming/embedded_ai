#include <string.h>
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "audio_capture.h"
#include "speaker.h"
#include "speech_frontend.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"

#define RATE 48000
#define SAMPLES 960
#define GAIN_DB 12
typedef struct __attribute__((packed)) {
    char magic[4];
    uint32_t sequence;
    uint64_t capture_us;
    uint32_t sample_rate;
    uint16_t samples;
    uint16_t flags;
    int16_t pcm[SAMPLES];
} audio_packet_t;
_Static_assert(sizeof(audio_packet_t) == 24 + SAMPLES * 2, "Audio wire layout");

static const char *TAG = "microphone";
static i2c_master_bus_handle_t bus;
i2c_master_bus_handle_t audio_capture_i2c_bus(void) {return bus;}
static esp_codec_dev_handle_t codec;
static QueueHandle_t packets;
static volatile bool ready, streaming;
static uint32_t sequence, read_errors, stream_drops, idle_overwrites;
static int alc_reg = -1, gain_reg = -1, volume_reg = -1, reference_reg = -1;

static void capture_task(void *arg)
{
    int16_t stereo[SAMPLES * 2];
    audio_packet_t packet = {.magic = {'B','L','A','1'}, .sample_rate = RATE, .samples = SAMPLES};
    for (;;) {
        int ret = esp_codec_dev_read(codec, stereo, sizeof(stereo));
        if (ret != ESP_CODEC_DEV_OK) {
            read_errors++;
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        packet.sequence = sequence++;
        packet.capture_us = esp_timer_get_time() - (SAMPLES * 1000000ULL / RATE);
        speech_frontend_feed(stereo, packet.sequence, packet.capture_us);
        packet.flags = 0;
        for (int n = 0; n < SAMPLES; n++) {
            packet.pcm[n] = stereo[n * 2]; // Raw WAV is ADC left only; DAC right is speech reference.
            if (packet.pcm[n] >= 32760 || packet.pcm[n] <= -32760) packet.flags |= 1;
        }
        if (xQueueSend(packets, &packet, 0) != pdPASS) {
            audio_packet_t discarded;
            xQueueReceive(packets, &discarded, 0);
            if (streaming) stream_drops++; else idle_overwrites++;
            xQueueSend(packets, &packet, 0);
        }
    }
}

esp_err_t audio_capture_init(void)
{
    // Stay muted until an explicit voice playback session begins.
    ESP_RETURN_ON_ERROR(gpio_set_direction(GPIO_NUM_53, GPIO_MODE_OUTPUT), TAG, "PA direction");
    ESP_RETURN_ON_ERROR(gpio_set_level(GPIO_NUM_53, 0), TAG, "PA mute");
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0, .sda_io_num = GPIO_NUM_7, .scl_io_num = GPIO_NUM_8,
        .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus_cfg, &bus), TAG, "I2C bus");
    ESP_RETURN_ON_ERROR(i2c_master_probe(bus, 0x18, 200), TAG, "ES8311 at 0x18 not detected");
    i2s_chan_handle_t tx, rx;
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    channel.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&channel, &tx, &rx), TAG, "I2S channels");
    i2s_std_config_t i2s = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = GPIO_NUM_13, .bclk = GPIO_NUM_12, .ws = GPIO_NUM_10,
                     .dout = GPIO_NUM_9, .din = GPIO_NUM_11},
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(tx, &i2s), TAG, "I2S TX");
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(rx, &i2s), TAG, "I2S RX");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(tx), TAG, "I2S TX enable");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(rx), TAG, "I2S RX enable");
    audio_codec_i2s_cfg_t data_cfg = {.port = 0, .rx_handle = rx, .tx_handle = tx};
    const audio_codec_data_if_t *data = audio_codec_new_i2s_data(&data_cfg);
    audio_codec_i2c_cfg_t control_cfg = {.port = 0, .addr = ES8311_CODEC_DEFAULT_ADDR,
                                        .bus_handle = bus, .clock_speed_hz = 100000};
    const audio_codec_ctrl_if_t *control = audio_codec_new_i2c_ctrl(&control_cfg);
    const audio_codec_gpio_if_t *gpio = audio_codec_new_gpio();
    ESP_RETURN_ON_FALSE(data && control && gpio, ESP_ERR_NO_MEM, TAG, "Codec adapters");
    es8311_codec_cfg_t codec_cfg = {.ctrl_if = control, .gpio_if = gpio,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH, .pa_pin = -1, .use_mclk = true,
        .digital_mic = false, .no_dac_ref = false, .mclk_div = 256};
    const audio_codec_if_t *device = es8311_codec_new(&codec_cfg);
    ESP_RETURN_ON_FALSE(device, ESP_FAIL, TAG, "ES8311 codec");
    esp_codec_dev_cfg_t config = {.codec_if = device, .data_if = data, .dev_type = ESP_CODEC_DEV_TYPE_IN_OUT};
    codec = esp_codec_dev_new(&config);
    ESP_RETURN_ON_FALSE(codec, ESP_FAIL, TAG, "Codec device");
    esp_codec_dev_sample_info_t format = {.sample_rate = RATE, .channel = 2, .bits_per_sample = 16};
    ESP_RETURN_ON_FALSE(esp_codec_dev_open(codec, &format) == 0, ESP_FAIL, TAG, "Open capture");
    ESP_RETURN_ON_FALSE(esp_codec_dev_set_in_gain(codec, GAIN_DB) == 0, ESP_FAIL, TAG, "Fixed gain");
    // ES8311 register 0x18 controls ALC enable/window size. Disable ALC explicitly.
    ESP_RETURN_ON_FALSE(esp_codec_dev_write_reg(codec, 0x18, 0) == 0, ESP_FAIL, TAG, "Disable ALC");
    esp_codec_dev_read_reg(codec, 0x18, &alc_reg);
    esp_codec_dev_read_reg(codec, 0x16, &gain_reg);
    esp_codec_dev_read_reg(codec, 0x17, &volume_reg);
    esp_codec_dev_read_reg(codec, 0x44, &reference_reg);
    ESP_RETURN_ON_FALSE(alc_reg == 0, ESP_FAIL, TAG, "ALC readback");
    // Up to 5.12s of bounded PSRAM capture buffering absorbs WLAN stalls.
    // Browser monitoring can trim latency independently; WAV retains received data.
    packets = xQueueCreateWithCaps(256, sizeof(audio_packet_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    ESP_RETURN_ON_FALSE(packets, ESP_ERR_NO_MEM, TAG, "Capture queue");
    ESP_RETURN_ON_FALSE(xTaskCreate(capture_task, "mic_capture", 16384, NULL, 8, NULL) == pdPASS,
                        ESP_ERR_NO_MEM, TAG, "Capture task");
    ready = true;
    ESP_RETURN_ON_ERROR(speaker_init(codec, tx), TAG, "Speaker init");
    esp_err_t speech_error = reference_reg == 0x58 ? speech_frontend_init() : ESP_FAIL;
    if (speech_error != ESP_OK) ESP_LOGE(TAG, "Speech frontend unavailable: %s", esp_err_to_name(speech_error));
    ESP_LOGI(TAG, "ES8311: %d Hz, PCM16 mono, gain=%d dB, ALC=0x%02x, PA disabled", RATE, GAIN_DB, alc_reg);
    return ESP_OK;
}

cJSON *audio_capture_status(void)
{
    cJSON *status = cJSON_CreateObject();
    cJSON_AddBoolToObject(status, "ready", ready);
    cJSON_AddBoolToObject(status, "streaming", streaming);
    cJSON_AddStringToObject(status, "source", "real_microphone");
    cJSON_AddNumberToObject(status, "sample_rate", RATE);
    cJSON_AddNumberToObject(status, "bits_per_sample", 16);
    cJSON_AddNumberToObject(status, "channels", 1);
    cJSON_AddNumberToObject(status, "gain_db", GAIN_DB);
    cJSON_AddNumberToObject(status, "sequence", sequence);
    cJSON_AddNumberToObject(status, "read_errors", read_errors);
    cJSON_AddNumberToObject(status, "stream_drops", stream_drops);
    cJSON_AddNumberToObject(status, "idle_overwrites", idle_overwrites);
    cJSON_AddNumberToObject(status, "alc_register", alc_reg);
    cJSON_AddNumberToObject(status, "gain_register", gain_reg);
    cJSON_AddNumberToObject(status, "adc_volume_register", volume_reg);
    cJSON_AddNumberToObject(status, "dac_reference_register", reference_reg);
    cJSON_AddBoolToObject(status, "speaker_enabled", speaker_enabled());
    return status;
}

static esp_err_t audio_handler(httpd_req_t *request)
{
    if (!ready) {
        httpd_resp_set_status(request, "503 Service Unavailable");
        return httpd_resp_sendstr(request, "Audio unavailable");
    }
    int no_delay = 1;
    setsockopt(httpd_req_to_sockfd(request), IPPROTO_TCP, TCP_NODELAY, &no_delay, sizeof(no_delay));
    httpd_resp_set_type(request, "application/octet-stream");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    xQueueReset(packets);
    stream_drops = 0;
    streaming = true;
    audio_packet_t packet;
    esp_err_t ret = ESP_OK;
    while (ret == ESP_OK) {
        if (xQueueReceive(packets, &packet, pdMS_TO_TICKS(2000)) != pdPASS) break;
        ret = httpd_resp_send_chunk(request, (const char *)&packet, sizeof(packet));
    }
    streaming = false;
    return ret;
}

esp_err_t audio_server_start(void)
{
    httpd_handle_t server;
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 81;
    config.ctrl_port = 32769;
    config.stack_size = 8192;
    config.max_open_sockets = 2;
    config.send_wait_timeout = 8;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &config), TAG, "Audio server");
    const httpd_uri_t uri = {.uri = "/audio", .method = HTTP_GET, .handler = audio_handler};
    return httpd_register_uri_handler(server, &uri);
}
