idf_component_register(INCLUDE_DIRS "include" REQUIRES esp_common heap)

if(NOT CONFIG_IDF_TARGET_ESP32P4 OR CONFIG_ESP32P4_SELECTS_REV_LESS_V3)
    message(FATAL_ERROR "This verified rate-conversion library is for ESP32-P4 revision >= 3 only")
endif()

file(READ "${CMAKE_CURRENT_SOURCE_DIR}/UPSTREAM.json" upstream_metadata)
foreach(upstream_file LICENSE include/esp_ae_rate_cvt.h include/esp_ae_types.h
        lib/esp32p4/libesp_audio_effects.a)
    string(JSON expected_hash GET "${upstream_metadata}" files "${upstream_file}" hash)
    file(SHA256 "${CMAKE_CURRENT_SOURCE_DIR}/${upstream_file}" actual_hash)
    if(NOT actual_hash STREQUAL expected_hash)
        message(FATAL_ERROR "Audio effects registry SHA-256 mismatch: ${upstream_file}")
    endif()
endforeach()

add_prebuilt_library(benchlink_audio_effects
    "${CMAKE_CURRENT_SOURCE_DIR}/lib/esp32p4/libesp_audio_effects.a")
target_link_libraries(${COMPONENT_LIB} INTERFACE benchlink_audio_effects)
