idf_component_register(INCLUDE_DIRS "include/esp32p4"
    REQUIRES esp_common freertos heap log esp_timer pthread espressif__dl_fft)

if(NOT CONFIG_IDF_TARGET_ESP32P4 OR CONFIG_ESP32P4_SELECTS_REV_LESS_V3)
    message(FATAL_ERROR "This verified DSP library is for ESP32-P4 revision >= 3 only")
endif()

file(READ "${CMAKE_CURRENT_SOURCE_DIR}/UPSTREAM.json" upstream_metadata)
foreach(upstream_file LICENSE include/esp32p4/esp_aec.h include/esp32p4/esp_aec_nlp.h
        include/esp32p4/esp_vad.h lib/esp32p4/libesp_audio_processor.a lib/esp32p4/libdl_lib.a)
    string(JSON expected_hash GET "${upstream_metadata}" files "${upstream_file}" hash)
    file(SHA256 "${CMAKE_CURRENT_SOURCE_DIR}/${upstream_file}" actual_hash)
    if(NOT actual_hash STREQUAL expected_hash)
        message(FATAL_ERROR "ESP-SR registry SHA-256 mismatch: ${upstream_file}")
    endif()
endforeach()

add_prebuilt_library(benchlink_sr_processor
    "${CMAKE_CURRENT_SOURCE_DIR}/lib/esp32p4/libesp_audio_processor.a"
    PRIV_REQUIRES espressif__dl_fft)
add_prebuilt_library(benchlink_sr_support
    "${CMAKE_CURRENT_SOURCE_DIR}/lib/esp32p4/libdl_lib.a"
    PRIV_REQUIRES espressif__dl_fft)
target_link_libraries(${COMPONENT_LIB} INTERFACE benchlink_sr_processor benchlink_sr_support)
