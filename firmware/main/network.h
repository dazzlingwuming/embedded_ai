#pragma once
#include "esp_err.h"
#include "cJSON.h"
esp_err_t gateway_wifi_connect(void);
void gateway_wifi_refresh_status(void);
cJSON *gateway_wifi_status(void);
