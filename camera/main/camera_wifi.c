#include <string.h>
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "camera_wifi.h"
#include "wifi_credentials.h"

static const char *TAG = "camera_wifi";
static EventGroupHandle_t events;
static size_t network_index;
static unsigned retries;

static void configure_network(void)
{
    wifi_config_t config = {0};
    memcpy(config.sta.ssid, known_wifi[network_index].ssid,
           strlen(known_wifi[network_index].ssid));
    memcpy(config.sta.password, known_wifi[network_index].password,
           strlen(known_wifi[network_index].password));
    config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    config.sta.pmf_cfg.capable = true;
    config.sta.pmf_cfg.required = false;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_LOGI(TAG, "Trying saved network %u/%u", (unsigned)network_index + 1,
             (unsigned)KNOWN_WIFI_COUNT);
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        ESP_ERROR_CHECK(esp_wifi_connect());
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(events, BIT0);
        const wifi_event_sta_disconnected_t *event = data;
        ESP_LOGW(TAG, "Disconnected (reason %u), reconnecting", event->reason);
        if (++retries >= 3) {
            retries = 0;
            network_index = (network_index + 1) % KNOWN_WIFI_COUNT;
            configure_network();
        }
        ESP_ERROR_CHECK(esp_wifi_connect());
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = data;
        retries = 0;
        ESP_LOGI(TAG, "Camera address: http://" IPSTR "/", IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(events, BIT0);
    }
}

esp_err_t camera_wifi_connect(void)
{
    events = xEventGroupCreate();
    if (!events) return ESP_ERR_NO_MEM;
    if (!esp_netif_create_default_wifi_sta()) return ESP_ERR_NO_MEM;
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    configure_network();
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    while (!(xEventGroupWaitBits(events, BIT0, pdFALSE, pdTRUE,
                                pdMS_TO_TICKS(30000)) & BIT0)) {
        ESP_LOGW(TAG, "Waiting for a saved 2.4 GHz network. Keep the hotspot enabled.");
    }
    return ESP_OK;
}
