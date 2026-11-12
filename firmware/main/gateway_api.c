#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_http_server.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "gateway_api.h"
#include "audio_capture.h"
#include "speaker.h"
#include "video_camera.h"
#include "network.h"
#include "speech_frontend.h"

#define DEVICE_COUNT 4
#define JOB_COUNT 16
#define DEVICE_TTL 6000
typedef struct {bool used; char id[33], name[65], session[65]; int64_t seen;} instrument_t;
typedef struct {
    bool used, late;
    char id[65], device[33], session[65], state[16], reason[80];
    int64_t created, deadline, updated;
    cJSON *request, *result;
} job_t;
static instrument_t devices[DEVICE_COUNT];
static job_t jobs[JOB_COUNT];
static char boot_id[17], board_id[13];
static int64_t now_ms(void) {return esp_timer_get_time() / 1000;}
static const char *str(cJSON *object, const char *key)
{
    const char *s = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(object, key));
    return s ? s : "";
}
static bool identifier(const char *s, size_t limit)
{
    if (!*s || strlen(s) > limit) return false;
    for (; *s; s++) if (!isalnum((unsigned char)*s) && *s != '-' && *s != '_' && *s != '.') return false;
    return true;
}
static esp_err_t reply(httpd_req_t *r, cJSON *json)
{
    // Small RPC replies must not wait for a delayed TCP acknowledgement while
    // the single HTTP task is also serving four instrument polling sessions.
    int no_delay = 1;
    setsockopt(httpd_req_to_sockfd(r), IPPROTO_TCP, TCP_NODELAY, &no_delay, sizeof(no_delay));
    char *text = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);
    if (!text) return ESP_ERR_NO_MEM;
    httpd_resp_set_type(r, "application/json");
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    esp_err_t ret = httpd_resp_sendstr(r, text);
    free(text);
    return ret;
}
static esp_err_t error(httpd_req_t *r, const char *status, const char *message)
{
    httpd_resp_set_status(r, status);
    cJSON *json = cJSON_CreateObject();
    cJSON_AddStringToObject(json, "error", message);
    return reply(r, json);
}
static cJSON *body(httpd_req_t *r)
{
    if (!r->content_len || r->content_len > 8192) return NULL;
    char *text = calloc(1, r->content_len + 1);
    if (!text) return NULL;
    int have = 0;
    while (have < r->content_len) {
        int n = httpd_req_recv(r, text + have, r->content_len - have);
        if (n <= 0) {free(text); return NULL;}
        have += n;
    }
    cJSON *json = cJSON_Parse(text);
    free(text);
    if (!cJSON_IsObject(json)) {cJSON_Delete(json); return NULL;}
    return json;
}
static instrument_t *find_device(const char *id)
{
    for (int n = 0; n < DEVICE_COUNT; n++) if (devices[n].used && !strcmp(devices[n].id, id)) return &devices[n];
    return NULL;
}
static job_t *find_job(const char *id)
{
    for (int n = 0; n < JOB_COUNT; n++) if (jobs[n].used && !strcmp(jobs[n].id, id)) return &jobs[n];
    return NULL;
}
static bool terminal(job_t *job)
{
    return strcmp(job->state, "queued") && strcmp(job->state, "received");
}
static void transition(job_t *job, const char *state, const char *reason)
{
    snprintf(job->state, sizeof(job->state), "%s", state);
    snprintf(job->reason, sizeof(job->reason), "%s", reason ? reason : "");
    job->updated = now_ms();
}
static void expire_jobs(void)
{
    for (int n = 0; n < JOB_COUNT; n++) {
        job_t *job = &jobs[n];
        if (job->used && !terminal(job) && now_ms() >= job->deadline)
            transition(job, !strcmp(job->state, "queued") ? "expired" : "unknown", "deadline_elapsed");
    }
}
static cJSON *job_json(job_t *job)
{
    cJSON *json = cJSON_CreateObject();
    cJSON_AddStringToObject(json, "request_id", job->id);
    cJSON_AddStringToObject(json, "device_id", job->device);
    cJSON_AddStringToObject(json, "source", "simulated");
    cJSON_AddStringToObject(json, "state", job->state);
    cJSON_AddStringToObject(json, "reason", job->reason);
    cJSON_AddStringToObject(json, "gateway_boot_id", boot_id);
    cJSON_AddNumberToObject(json, "gateway_created_ms", job->created);
    cJSON_AddNumberToObject(json, "gateway_updated_ms", job->updated);
    cJSON_AddNumberToObject(json, "gateway_deadline_ms", job->deadline);
    cJSON_AddBoolToObject(json, "late_result", job->late);
    cJSON_AddItemToObject(json, "request", cJSON_Duplicate(job->request, true));
    if (job->result) cJSON_AddItemToObject(json, "result", cJSON_Duplicate(job->result, true));
    return json;
}
static cJSON *device_list(void)
{
    cJSON *list = cJSON_CreateArray();
    for (int n = 0; n < DEVICE_COUNT; n++) {
        instrument_t *d = &devices[n];
        if (!d->used) continue;
        cJSON *j = cJSON_CreateObject();
        cJSON_AddStringToObject(j, "device_id", d->id);
        cJSON_AddStringToObject(j, "name", d->name);
        cJSON_AddStringToObject(j, "source", "simulated");
        cJSON_AddStringToObject(j, "transport", "network_simulator");
        cJSON_AddBoolToObject(j, "connected", now_ms() - d->seen < DEVICE_TTL);
        cJSON_AddNumberToObject(j, "last_seen_age_ms", now_ms() - d->seen);
        cJSON_AddItemToArray(list, j);
    }
    return list;
}
static esp_err_t status_handler(httpd_req_t *r)
{
    expire_jobs();
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "board_id", board_id);
    cJSON_AddStringToObject(j, "boot_id", boot_id);
    cJSON_AddStringToObject(j, "firmware", esp_app_get_description()->version);
    cJSON_AddStringToObject(j, "board", "Waveshare ESP32-P4-NANO");
    cJSON_AddNumberToObject(j, "uptime_ms", now_ms());
    cJSON_AddNumberToObject(j, "internal_heap_free", heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    cJSON_AddNumberToObject(j, "psram_free", heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    cJSON_AddItemToObject(j, "audio", audio_capture_status());
    cJSON_AddItemToObject(j, "speaker", speaker_status());
    cJSON_AddItemToObject(j, "camera", video_camera_status());
    cJSON_AddItemToObject(j, "speech_frontend", speech_frontend_status());
    cJSON_AddItemToObject(j, "network", gateway_wifi_status());
    cJSON_AddStringToObject(j, "mode", "multimedia");
    cJSON_AddItemToObject(j, "devices", device_list());
    cJSON_AddBoolToObject(j, "video_enabled", video_camera_ready());
    cJSON_AddBoolToObject(j, "physical_instruments_verified", false);
    return reply(r, j);
}
static esp_err_t register_handler(httpd_req_t *r)
{
    cJSON *j = body(r);
    const char *id = str(j, "device_id"), *session = str(j, "session_id");
    if (!j || !identifier(id, 32) || !identifier(session, 64) || strcmp(str(j, "source"), "simulated")) {
        cJSON_Delete(j); return error(r, "400 Bad Request", "Invalid simulated device registration");
    }
    instrument_t *d = find_device(id);
    if (!d) for (int n = 0; n < DEVICE_COUNT; n++) if (!devices[n].used) {d = &devices[n]; break;}
    if (!d) {cJSON_Delete(j); return error(r, "429 Too Many Requests", "Four simulator slots are in use");}
    if (d->used && strcmp(d->session, session)) {
        for (int n = 0; n < JOB_COUNT; n++) if (jobs[n].used && !strcmp(jobs[n].device, id) && !terminal(&jobs[n]))
            transition(&jobs[n], !strcmp(jobs[n].state, "queued") ? "expired" : "unknown", "device_session_changed");
    }
    d->used = true; d->seen = now_ms();
    snprintf(d->id, sizeof(d->id), "%s", id);
    snprintf(d->session, sizeof(d->session), "%s", session);
    snprintf(d->name, sizeof(d->name), "%s", str(j, "name"));
    cJSON_Delete(j);
    cJSON *out = cJSON_CreateObject();
    cJSON_AddBoolToObject(out, "registered", true);
    cJSON_AddStringToObject(out, "gateway_boot_id", boot_id);
    return reply(r, out);
}
static esp_err_t submit_handler(httpd_req_t *r)
{
    expire_jobs();
    cJSON *j = body(r);
    const char *id = str(j, "request_id"), *device = str(j, "device_id"), *method = str(j, "method");
    cJSON *ttl = cJSON_GetObjectItemCaseSensitive(j, "ttl_ms");
    bool allowed = !strcmp(method, "identify") || !strcmp(method, "read_status") || !strcmp(method, "read_waveform") ||
                   !strcmp(method, "set_parameter") || !strcmp(method, "set_fault");
    if (!j || !identifier(id, 64) || !identifier(device, 32) || !allowed || !cJSON_IsNumber(ttl) ||
        !isfinite(ttl->valuedouble) || ttl->valuedouble < 200 || ttl->valuedouble > 30000) {
        cJSON_Delete(j); return error(r, "400 Bad Request", "Invalid command, method, or TTL");
    }
    job_t *job = find_job(id);
    if (job) {
        bool same = cJSON_Compare(job->request, j, true);
        cJSON_Delete(j);
        if (!same) return error(r, "409 Conflict", "request_id_conflict");
        return reply(r, job_json(job)); // Never dispatch the same request twice.
    }
    instrument_t *d = find_device(device);
    if (!d || now_ms() - d->seen >= DEVICE_TTL) {cJSON_Delete(j); return error(r, "503 Service Unavailable", "device_offline");}
    job = NULL;
    for (int n = 0; n < JOB_COUNT; n++) if (!jobs[n].used) {job = &jobs[n]; break;}
    if (!job) for (int n = 0; n < JOB_COUNT; n++) if (terminal(&jobs[n]) && (!job || jobs[n].updated < job->updated)) job = &jobs[n];
    if (!job) {cJSON_Delete(j); return error(r, "429 Too Many Requests", "command_queue_full");}
    cJSON_Delete(job->request); cJSON_Delete(job->result);
    memset(job, 0, sizeof(*job));
    job->used = true; job->request = j;
    snprintf(job->id, sizeof(job->id), "%s", id);
    snprintf(job->device, sizeof(job->device), "%s", device);
    snprintf(job->session, sizeof(job->session), "%s", d->session);
    job->created = now_ms(); job->deadline = job->created + ttl->valueint;
    transition(job, "queued", "");
    return reply(r, job_json(job));
}
static esp_err_t command_handler(httpd_req_t *r)
{
    expire_jobs();
    char id[65];
    snprintf(id, sizeof(id), "%s", r->uri + strlen("/api/commands/"));
    job_t *job = find_job(id);
    if (!job) return error(r, "404 Not Found", "unknown_request_id");
    return reply(r, job_json(job));
}
static esp_err_t poll_handler(httpd_req_t *r)
{
    expire_jobs();
    char query[200], id[33], session[65];
    if (httpd_req_get_url_query_str(r, query, sizeof(query)) != ESP_OK ||
        httpd_query_key_value(query, "device_id", id, sizeof(id)) != ESP_OK ||
        httpd_query_key_value(query, "session_id", session, sizeof(session)) != ESP_OK)
        return error(r, "400 Bad Request", "Missing simulator identity");
    instrument_t *d = find_device(id);
    if (!d || strcmp(session, d->session)) return error(r, "409 Conflict", "register_required");
    d->seen = now_ms();
    job_t *first = NULL;
    for (int n = 0; n < JOB_COUNT; n++) {
        job_t *job = &jobs[n];
        if (job->used && !strcmp(job->state, "queued") && !strcmp(job->device, id) && !strcmp(job->session, session) &&
            (!first || job->created < first->created)) first = job;
    }
    cJSON *out = cJSON_CreateObject();
    if (first) {
        transition(first, "received", "");
        cJSON *command = cJSON_Duplicate(first->request, true);
        cJSON_AddNumberToObject(command, "remaining_ms", first->deadline - now_ms());
        cJSON_AddItemToObject(out, "command", command);
    } else cJSON_AddNullToObject(out, "command");
    cJSON_AddStringToObject(out, "gateway_boot_id", boot_id);
    return reply(r, out);
}
static esp_err_t result_handler(httpd_req_t *r)
{
    expire_jobs();
    cJSON *j = body(r);
    job_t *job = find_job(str(j, "request_id"));
    const char *state = str(j, "state");
    if (!j || !job || strcmp(str(j, "device_id"), job->device) || strcmp(str(j, "session_id"), job->session) ||
        strcmp(str(j, "source"), "simulated") ||
        (strcmp(state, "completed") && strcmp(state, "applied") && strcmp(state, "rejected"))) {
        cJSON_Delete(j); return error(r, "400 Bad Request", "Invalid simulator result");
    }
    if (strcmp(job->state, "received") && strcmp(job->state, "unknown")) {
        // An acknowledgement response can itself be lost. Repeating the exact
        // result confirms it without dispatching or executing the command again.
        bool same = !strcmp(job->state, state) &&
            cJSON_Compare(job->result, cJSON_GetObjectItemCaseSensitive(j, "result"), true);
        cJSON_Delete(j);
        if (same) return reply(r, job_json(job));
        return error(r, "409 Conflict", "Command is not awaiting a result");
    }
    job->late = !strcmp(job->state, "unknown");
    transition(job, state, "");
    job->result = cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(j, "result"), true);
    cJSON_Delete(j);
    return reply(r, job_json(job));
}
static esp_err_t cancel_speaker_handler(httpd_req_t *r)
{
    cJSON *json = body(r);
    if (!json) return error(r, "400 Bad Request", "Expected JSON body");
    cJSON_Delete(json);
    speaker_cancel();
    return reply(r, speaker_status());
}

esp_err_t gateway_api_start(void)
{
    uint8_t mac[6] = {0};
    // P4 has no native Wi-Fi MAC type; identify the P4 by its eFuse base MAC.
    ESP_ERROR_CHECK(esp_efuse_mac_get_default(mac));
    snprintf(board_id, sizeof(board_id), "%02x%02x%02x%02x%02x%02x", mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
    snprintf(boot_id, sizeof(boot_id), "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random());
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.stack_size = 10240; config.max_uri_handlers = 16;
    config.max_open_sockets = 12; config.lru_purge_enable = true;
    config.uri_match_fn = httpd_uri_match_wildcard;
    httpd_handle_t server;
    esp_err_t ret = httpd_start(&server, &config);
    if (ret != ESP_OK) return ret;
    const httpd_uri_t routes[] = {
        {.uri="/api/status", .method=HTTP_GET, .handler=status_handler},
        {.uri="/api/speaker/cancel", .method=HTTP_POST, .handler=cancel_speaker_handler},
        {.uri="/api/sim/register", .method=HTTP_POST, .handler=register_handler},
        {.uri="/api/sim/poll", .method=HTTP_GET, .handler=poll_handler},
        {.uri="/api/sim/result", .method=HTTP_POST, .handler=result_handler},
        {.uri="/api/commands", .method=HTTP_POST, .handler=submit_handler},
        {.uri="/api/commands/*", .method=HTTP_GET, .handler=command_handler},
    };
    for (size_t n=0; n<sizeof(routes)/sizeof(routes[0]); n++) {
        ret = httpd_register_uri_handler(server, &routes[n]);
        if (ret != ESP_OK) return ret;
    }
    return video_camera_register(server);
}
