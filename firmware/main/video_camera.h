#pragma once
#include "esp_err.h"
#include "esp_http_server.h"
#include "driver/i2c_master.h"
#include "cJSON.h"
esp_err_t video_camera_init(i2c_master_bus_handle_t bus);
esp_err_t video_camera_register(httpd_handle_t server);
esp_err_t video_camera_server_start(void);
bool video_camera_ready(void);
cJSON *video_camera_status(void);
