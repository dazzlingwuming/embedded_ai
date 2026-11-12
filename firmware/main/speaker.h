#pragma once
#include <stdbool.h>
#include "cJSON.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_err.h"

esp_err_t speaker_init(esp_codec_dev_handle_t codec, i2s_chan_handle_t tx);
esp_err_t speaker_server_start(void);
void speaker_cancel(void);
bool speaker_enabled(void);
cJSON *speaker_status(void);
