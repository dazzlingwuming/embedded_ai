#pragma once
#include <stdint.h>
#include "esp_err.h"
#include "cJSON.h"

esp_err_t speech_frontend_init(void);
void speech_frontend_feed(const int16_t *stereo, uint32_t sequence, uint64_t capture_us);
esp_err_t speech_frontend_server_start(void);
cJSON *speech_frontend_status(void);
