#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "network.h"
#include "audio_capture.h"
#include "gateway_api.h"
#include "speaker.h"
#include "video_camera.h"
#include "speech_frontend.h"

static void diagnostics_task(void *arg)
{
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        gateway_wifi_refresh_status();
        cJSON *audio = audio_capture_status();
        ESP_LOGI("gateway_health", "seq=%lu read_errors=%lu stream_drops=%lu free_internal=%u",
            (unsigned long)cJSON_GetObjectItem(audio, "sequence")->valuedouble,
            (unsigned long)cJSON_GetObjectItem(audio, "read_errors")->valuedouble,
            (unsigned long)cJSON_GetObjectItem(audio, "stream_drops")->valuedouble,
            heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        cJSON_Delete(audio);
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(gateway_wifi_connect());
    // Expose diagnostics even if the audio codec needs attention.
    err = audio_capture_init();
    if (err != ESP_OK) ESP_LOGE("gateway", "Audio initialization failed: %s", esp_err_to_name(err));
    esp_err_t camera_error = video_camera_init(audio_capture_i2c_bus());
    if (camera_error != ESP_OK) ESP_LOGE("gateway", "Camera initialization failed: %s", esp_err_to_name(camera_error));
    ESP_ERROR_CHECK(gateway_api_start());
    if (err == ESP_OK) ESP_ERROR_CHECK(audio_server_start());
    if (err == ESP_OK) ESP_ERROR_CHECK(speaker_server_start());
    if (err == ESP_OK) ESP_ERROR_CHECK(speech_frontend_server_start());
    if (camera_error == ESP_OK) ESP_ERROR_CHECK(video_camera_server_start());
    xTaskCreate(diagnostics_task, "health", 4096, NULL, 2, NULL);
    ESP_LOGI("gateway", "BenchLink multimedia gateway ready; speaker muted until explicit playback");
}
