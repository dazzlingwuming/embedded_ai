/* A separate speech-only path. The raw 48 kHz ADC/WAV path is unchanged. */
#include <math.h>
#include <stddef.h>
#include <string.h>
#include "esp_aec.h"
#include "esp_vad.h"
#include "esp_ae_rate_cvt.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "speaker.h"
#include "speech_frontend.h"

#define RAW_SAMPLES 960
#define VOICE_SAMPLES 320
#define VOICE_RATE 16000
#define AEC_SAMPLES 512
typedef struct {
    uint64_t capture_us;
    uint32_t sequence, generation;
    int16_t stereo[RAW_SAMPLES * 2];
} input_t;
typedef struct __attribute__((aligned(16))) {
    char magic[4];
    uint32_t sequence;
    uint64_t capture_us;
    uint32_t sample_rate;
    uint16_t samples, flags, mic_rms, ref_rms, clean_rms, speech_ms;
    int16_t pcm[VOICE_SAMPLES], mic[VOICE_SAMPLES], ref[VOICE_SAMPLES];
} packet_t;
_Static_assert(sizeof(packet_t) == 32 + VOICE_SAMPLES * 6, "Speech diagnostic wire layout");
_Static_assert(offsetof(packet_t, pcm) == 32, "Speech header size");

static const char *TAG = "speech_frontend";
static QueueHandle_t inputs, outputs;
static volatile bool ready, streaming;
static volatile uint32_t stream_generation, input_drops, output_drops, process_errors, processed;
static volatile uint16_t last_mic_rms, last_ref_rms, last_clean_rms, last_speech_ms;
static aec_handle_t *aec;
static vad_handle_t vad;
static esp_ae_rate_cvt_handle_t resampler;
static int16_t *mic_frame, *ref_frame, *clean_frame, *converted;
static uint32_t converted_capacity;

static aec_handle_t *new_aec(void)
{
    aec_config_t config = {.mic_num = 1, .ref_num = 1, .out_num = 1,
        .filter_length = 4, .sample_rate = VOICE_RATE,
        .caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT,
        .mode = AEC_MODE_FD_LOW_COST, .nlp_level = AEC_NLP_LEVEL_AGGR};
    return aec_create_from_config(&config);
}

void speech_frontend_feed(const int16_t *stereo, uint32_t sequence, uint64_t capture_us)
{
    if (!ready || !streaming) return;
    input_t input = {.capture_us = capture_us, .sequence = sequence, .generation = stream_generation};
    memcpy(input.stereo, stereo, sizeof(input.stereo));
    if (xQueueSend(inputs, &input, 0) != pdPASS) input_drops++;
}

static void process_task(void *argument)
{
    input_t input;
    packet_t packet = {.magic = {'B','L','V','1'}, .sample_rate = VOICE_RATE, .samples = VOICE_SAMPLES};
    uint32_t generation = 0, previous = 0;
    uint64_t origin_us = 0;
    int aec_count = 0, voice_count = 0, speech_ms = 0;
    bool have_previous = false, discontinuity = false;
    for (;;) {
        if (xQueueReceive(inputs, &input, pdMS_TO_TICKS(200)) != pdPASS) continue;
        uint32_t current = stream_generation;
        if (!streaming || input.generation != current) continue;
        if (generation != current) {
            generation = current;
            aec_destroy(aec); aec = new_aec();
            if (!aec || aec_get_chunksize(aec) != AEC_SAMPLES) {
                process_errors++; ready = false; streaming = false; continue;
            }
            esp_ae_rate_cvt_reset(resampler);
            vad_reset_trigger(vad);
            aec_count = voice_count = speech_ms = 0;
            have_previous = discontinuity = false;
            packet.sequence = 0;
            origin_us = input.capture_us;
        }
        if (have_previous && input.sequence != previous + 1) discontinuity = true;
        previous = input.sequence; have_previous = true;
        uint32_t count = converted_capacity;
        if (esp_ae_rate_cvt_process(resampler, input.stereo, RAW_SAMPLES, converted, &count) != ESP_AE_ERR_OK) {
            process_errors++; discontinuity = true; continue;
        }
        for (uint32_t n = 0; n < count; n++) {
            mic_frame[aec_count] = converted[n*2];
            ref_frame[aec_count] = converted[n*2+1];
            if (++aec_count != AEC_SAMPLES) continue;
            aec_process(aec, mic_frame, ref_frame, clean_frame);
            aec_count = 0; processed++;
            for (int k = 0; k < AEC_SAMPLES; k++) {
                packet.pcm[voice_count] = clean_frame[k];
                packet.mic[voice_count] = mic_frame[k];
                packet.ref[voice_count] = ref_frame[k];
                if (++voice_count != VOICE_SAMPLES) continue;
                voice_count = 0;
                uint64_t mic_sum = 0, ref_sum = 0, clean_sum = 0;
                for (int j = 0; j < VOICE_SAMPLES; j++) {
                    mic_sum += (int32_t)packet.mic[j] * packet.mic[j];
                    ref_sum += (int32_t)packet.ref[j] * packet.ref[j];
                    clean_sum += (int32_t)packet.pcm[j] * packet.pcm[j];
                }
                bool speech = vad_process(vad, packet.pcm, VOICE_RATE, 20) == VAD_SPEECH;
                speech_ms = speech ? (speech_ms < 65000 ? speech_ms + 20 : speech_ms) : 0;
                packet.mic_rms = last_mic_rms = sqrt((double)mic_sum / VOICE_SAMPLES);
                packet.ref_rms = last_ref_rms = sqrt((double)ref_sum / VOICE_SAMPLES);
                packet.clean_rms = last_clean_rms = sqrt((double)clean_sum / VOICE_SAMPLES);
                packet.speech_ms = last_speech_ms = speech_ms;
                packet.flags = (speech ? 1 : 0) | (speaker_enabled() ? 2 : 0) | 4 | (discontinuity ? 8 : 0);
                packet.capture_us = origin_us + (uint64_t)packet.sequence * 20000;
                packet.sequence++;
                if (!streaming || generation != stream_generation) break;
                if (xQueueSend(outputs, &packet, 0) != pdPASS) {
                    packet_t discarded;
                    xQueueReceive(outputs, &discarded, 0);
                    output_drops++;
                    xQueueSend(outputs, &packet, 0);
                }
            }
        }
    }
}

esp_err_t speech_frontend_init(void)
{
    esp_ae_rate_cvt_cfg_t config = {.src_rate = 48000, .dest_rate = VOICE_RATE,
        .channel = 2, .bits_per_sample = 16, .complexity = 3,
        .perf_type = ESP_AE_RATE_CVT_PERF_TYPE_SPEED};
    ESP_RETURN_ON_FALSE(esp_ae_rate_cvt_open(&config, &resampler) == ESP_AE_ERR_OK,
                       ESP_FAIL, TAG, "Speech resampler");
    ESP_RETURN_ON_FALSE(esp_ae_rate_cvt_get_max_out_sample_num(resampler, RAW_SAMPLES,
                       &converted_capacity) == ESP_AE_ERR_OK && converted_capacity <= 2048,
                       ESP_FAIL, TAG, "Speech resampler size");
    int caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
    mic_frame = heap_caps_aligned_alloc(64, AEC_SAMPLES * 2, caps);
    ref_frame = heap_caps_aligned_alloc(64, AEC_SAMPLES * 2, caps);
    clean_frame = heap_caps_aligned_alloc(64, AEC_SAMPLES * 2, caps);
    converted = heap_caps_aligned_alloc(64, converted_capacity * 4, caps);
    inputs = xQueueCreateWithCaps(8, sizeof(input_t), caps);
    outputs = xQueueCreateWithCaps(64, sizeof(packet_t), caps);
    aec = new_aec();
    vad = vad_create_with_param(VAD_MODE_2, VOICE_RATE, 20, 240, 120);
    ESP_RETURN_ON_FALSE(mic_frame && ref_frame && clean_frame && converted && inputs && outputs && aec && vad,
                       ESP_ERR_NO_MEM, TAG, "Speech processing buffers");
    ESP_RETURN_ON_FALSE(aec_get_chunksize(aec) == AEC_SAMPLES, ESP_FAIL, TAG, "Unexpected AEC frame length");
    ESP_RETURN_ON_FALSE(xTaskCreatePinnedToCore(process_task, "speech_aec", 16384, NULL, 7, NULL, 1) == pdPASS,
                       ESP_ERR_NO_MEM, TAG, "Speech task");
    ready = true;
    ESP_LOGI(TAG, "ESP-SR FD AEC + WebRTC VAD ready; MR reference, 16kHz speech, raw ADC unchanged");
    return ESP_OK;
}

static esp_err_t speech_handler(httpd_req_t *request)
{
    if (!ready || streaming) {
        httpd_resp_set_status(request, "503 Service Unavailable");
        return httpd_resp_sendstr(request, "Speech frontend unavailable or busy");
    }
    char query[32] = {0};
    httpd_req_get_url_query_str(request, query, sizeof(query));
    bool diagnostic = strstr(query, "debug=1") != NULL;
    int no_delay = 1;
    setsockopt(httpd_req_to_sockfd(request), IPPROTO_TCP, TCP_NODELAY, &no_delay, sizeof(no_delay));
    httpd_resp_set_type(request, "application/octet-stream");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    xQueueReset(inputs); xQueueReset(outputs);
    input_drops = output_drops = 0;
    stream_generation++; streaming = true;
    packet_t packet;
    esp_err_t result = ESP_OK;
    while (ready && result == ESP_OK) {
        if (xQueueReceive(outputs, &packet, pdMS_TO_TICKS(2000)) != pdPASS) break;
        if (diagnostic) memcpy(packet.magic, "BLD1", 4);
        result = httpd_resp_send_chunk(request, (char *)&packet,
            diagnostic ? sizeof(packet) : 32 + VOICE_SAMPLES * 2);
    }
    streaming = false;
    return result;
}

esp_err_t speech_frontend_server_start(void)
{
    if (!ready) return ESP_OK;
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 84; config.ctrl_port = 32772;
    config.stack_size = 6144; config.max_open_sockets = 2; config.send_wait_timeout = 2;
    httpd_handle_t server;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &config), TAG, "Speech server");
    httpd_uri_t uri = {.uri = "/speech", .method = HTTP_GET, .handler = speech_handler};
    return httpd_register_uri_handler(server, &uri);
}

cJSON *speech_frontend_status(void)
{
    cJSON *json = cJSON_CreateObject();
    cJSON_AddBoolToObject(json, "ready", ready);
    cJSON_AddBoolToObject(json, "streaming", streaming);
    cJSON_AddStringToObject(json, "algorithm", "ESP-SR AEC_MODE_FD_LOW_COST + WebRTC VAD");
    cJSON_AddStringToObject(json, "source", "aec_microphone");
    cJSON_AddNumberToObject(json, "sample_rate", VOICE_RATE);
    cJSON_AddNumberToObject(json, "port", 84);
    cJSON_AddNumberToObject(json, "processed_frames", processed);
    cJSON_AddNumberToObject(json, "input_drops", input_drops);
    cJSON_AddNumberToObject(json, "output_drops", output_drops);
    cJSON_AddNumberToObject(json, "process_errors", process_errors);
    cJSON_AddNumberToObject(json, "mic_rms", last_mic_rms);
    cJSON_AddNumberToObject(json, "reference_rms", last_ref_rms);
    cJSON_AddNumberToObject(json, "clean_rms", last_clean_rms);
    cJSON_AddNumberToObject(json, "speech_ms", last_speech_ms);
    return json;
}
