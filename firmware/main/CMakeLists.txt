idf_component_register(SRCS "main.c" "network.c" "audio_capture.c" "gateway_api.c" "speaker.c" "video_camera.c" "speech_frontend.c"
    INCLUDE_DIRS "." "../../local"
    REQUIRES esp_http_server esp_wifi esp_netif esp_event esp_timer nvs_flash json
             esp_driver_i2c esp_driver_i2s esp_driver_gpio esp_codec_dev esp_app_format esp_hw_support esp_video esp_driver_jpeg esp-sr esp_audio_effects)
