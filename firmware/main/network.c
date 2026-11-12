#include <string.h>
#include <stdio.h>
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_netif_ip_addr.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "network.h"
#include "wifi_credentials.h"

static const char *TAG = "gateway_wifi";
static EventGroupHandle_t events;
static size_t network_index;
static unsigned retries;
static char current_ssid[33], current_ip[16];
static int current_rssi, current_channel;
static bool current_ap_known;
static portMUX_TYPE status_lock = portMUX_INITIALIZER_UNLOCKED;

void gateway_wifi_refresh_status(void)
{
    wifi_ap_record_t record = {0};
    bool valid = esp_wifi_sta_get_ap_info(&record) == ESP_OK;
    portENTER_CRITICAL(&status_lock);
    current_ap_known = valid;
    if (valid) {
        memcpy(current_ssid, record.ssid, 32); current_ssid[32] = 0;
        current_rssi = record.rssi; current_channel = record.primary;
    }
    portEXIT_CRITICAL(&status_lock);
}

cJSON *gateway_wifi_status(void)
{
    char ssid[33], ip[16]; int rssi, channel; bool known;
    portENTER_CRITICAL(&status_lock);
    memcpy(ssid, current_ssid, sizeof(ssid)); memcpy(ip, current_ip, sizeof(ip));
    rssi = current_rssi; channel = current_channel; known = current_ap_known;
    portEXIT_CRITICAL(&status_lock);
    cJSON *json = cJSON_CreateObject();
    cJSON_AddBoolToObject(json, "connected", events && (xEventGroupGetBits(events) & BIT0));
    cJSON_AddStringToObject(json, "ssid", ssid);
    cJSON_AddStringToObject(json, "ipv4", ip);
    if (known) {cJSON_AddNumberToObject(json, "rssi_dbm", rssi); cJSON_AddNumberToObject(json, "channel", channel);}
    return json;
}

static void request_connection(void)
{
    esp_err_t err = esp_wifi_connect();
    if (err != ESP_OK) ESP_LOGW(TAG, "Connect not ready (%s); will retry", esp_err_to_name(err));
}

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
        request_connection();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(events, BIT0);
        const wifi_event_sta_disconnected_t *event = data;
        ESP_LOGW(TAG, "Disconnected (reason %u), reconnecting", event->reason);
        if (++retries >= 3) {
            retries = 0;
            network_index = (network_index + 1) % KNOWN_WIFI_COUNT;
            configure_network();
        }
        request_connection();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = data;
        retries = 0;
        ESP_LOGI(TAG, "Gateway address: http://" IPSTR "/", IP2STR(&event->ip_info.ip));
        portENTER_CRITICAL(&status_lock);
        snprintf(current_ip, sizeof(current_ip), IPSTR, IP2STR(&event->ip_info.ip));
        snprintf(current_ssid, sizeof(current_ssid), "%s", known_wifi[network_index].ssid);
        portEXIT_CRITICAL(&status_lock);
        xEventGroupSetBits(events, BIT0);
    }
}

esp_err_t gateway_wifi_connect(void)
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
                                pdMS_TO_TICKS(10000)) & BIT0)) {
        ESP_LOGW(TAG, "Waiting for a saved 2.4 GHz network. Keep the hotspot enabled.");
        request_connection();
    }
    gateway_wifi_refresh_status();
    return ESP_OK;
}
