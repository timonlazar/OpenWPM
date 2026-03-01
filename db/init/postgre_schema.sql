-- sql
/* PostgreSQL-adapted schema.sql
 * Converted types:
 * - App-supplied IDs use BIGINT (visit_id, browser_id, task_id)
 * - DB-assigned autoincrement columns use SERIAL (32-bit) or BIGSERIAL if needed
 * - DATETIME -> TIMESTAMP
 * - BIGINT flag columns -> BOOLEAN
 * - post_body_raw -> BYTEA
 * - STRING -> TEXT
 * Keep CREATE TABLE IF NOT EXISTS as before.
 */

CREATE TABLE IF NOT EXISTS task (
                                    task_id BIGINT PRIMARY KEY,
                                    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                    manager_params TEXT NOT NULL,
                                    openwpm_version TEXT NOT NULL,
                                    browser_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl (
                                     browser_id BIGINT PRIMARY KEY,
                                     task_id BIGINT NOT NULL REFERENCES task(task_id),
    browser_params TEXT NOT NULL,
    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

CREATE TABLE IF NOT EXISTS site_visits (
                                           visit_id BIGINT PRIMARY KEY,
                                           browser_id BIGINT NOT NULL REFERENCES crawl(browser_id),
    site_url TEXT NOT NULL,
    site_rank BIGINT
    );

CREATE TABLE IF NOT EXISTS crawl_history (
                                             browser_id BIGINT,
                                             visit_id BIGINT,
                                             command TEXT,
                                             arguments TEXT,
                                             retry_number BIGINT,
                                             command_status TEXT,
                                             error TEXT,
                                             traceback TEXT,
                                             duration BIGINT,
                                             dtg TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                             FOREIGN KEY (browser_id) REFERENCES crawl(browser_id)
    );

CREATE TABLE IF NOT EXISTS http_requests (
                                             id SERIAL PRIMARY KEY,
                                             incognito BOOLEAN,
                                             browser_id BIGINT NOT NULL,
                                             visit_id BIGINT NOT NULL,
                                             extension_session_uuid TEXT,
                                             event_ordinal BIGINT,
                                             window_id BIGINT,
                                             tab_id BIGINT,
                                             frame_id BIGINT,
                                             url TEXT NOT NULL,
                                             top_level_url TEXT,
                                             parent_frame_id BIGINT,
                                             frame_ancestors TEXT,
                                             method TEXT NOT NULL,
                                             referrer TEXT NOT NULL,
                                             headers TEXT NOT NULL,
                                             request_id BIGINT NOT NULL,
                                             is_XHR BOOLEAN,
                                             is_third_party_channel BOOLEAN,
                                             is_third_party_to_top_window BOOLEAN,
                                             triggering_origin TEXT,
                                             loading_origin TEXT,
                                             loading_href TEXT,
                                             req_call_stack TEXT,
                                             resource_type TEXT NOT NULL,
                                             post_body TEXT,
                                             post_body_raw BYTEA,
                                             time_stamp TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS http_responses (
                                              id SERIAL PRIMARY KEY,
                                              incognito BOOLEAN,
                                              browser_id BIGINT NOT NULL,
                                              visit_id BIGINT NOT NULL,
                                              extension_session_uuid TEXT,
                                              event_ordinal BIGINT,
                                              window_id BIGINT,
                                              tab_id BIGINT,
                                              frame_id BIGINT,
                                              url TEXT NOT NULL,
                                              method TEXT NOT NULL,
                                              response_status BIGINT,
                                              response_status_text TEXT,
                                              is_cached BOOLEAN NOT NULL,
                                              headers TEXT NOT NULL,
                                              request_id BIGINT NOT NULL,
                                              location TEXT,
                                              time_stamp TIMESTAMP NOT NULL,
                                              content_hash TEXT
);

CREATE TABLE IF NOT EXISTS http_redirects (
                                              id SERIAL PRIMARY KEY,
                                              incognito BOOLEAN,
                                              browser_id BIGINT NOT NULL,
                                              visit_id BIGINT NOT NULL,
                                              old_request_url TEXT,
                                              old_request_id TEXT,
                                              new_request_url TEXT,
                                              new_request_id TEXT,
                                              extension_session_uuid TEXT,
                                              event_ordinal BIGINT,
                                              window_id BIGINT,
                                              tab_id BIGINT,
                                              frame_id BIGINT,
                                              response_status BIGINT NOT NULL,
                                              response_status_text TEXT NOT NULL,
                                              headers TEXT NOT NULL,
                                              time_stamp TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS javascript (
                                          id SERIAL PRIMARY KEY,
                                          incognito BOOLEAN,
                                          browser_id BIGINT NOT NULL,
                                          visit_id BIGINT NOT NULL,
                                          extension_session_uuid TEXT,
                                          event_ordinal BIGINT,
                                          page_scoped_event_ordinal BIGINT,
                                          window_id BIGINT,
                                          tab_id BIGINT,
                                          frame_id BIGINT,
                                          script_url TEXT,
                                          script_line TEXT,
                                          script_col TEXT,
                                          func_name TEXT,
                                          script_loc_eval TEXT,
                                          document_url TEXT,
                                          top_level_url TEXT,
                                          call_stack TEXT,
                                          symbol TEXT,
                                          operation TEXT,
                                          value TEXT,
                                          arguments TEXT,
                                          time_stamp TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS javascript_cookies (
                                                  id SERIAL PRIMARY KEY,
                                                  browser_id BIGINT NOT NULL,
                                                  visit_id BIGINT NOT NULL,
                                                  extension_session_uuid TEXT,
                                                  event_ordinal BIGINT,
                                                  record_type TEXT,
                                                  change_cause TEXT,
                                                  expiry TIMESTAMP,
                                                  is_http_only BOOLEAN,
                                                  is_host_only BOOLEAN,
                                                  is_session BOOLEAN,
                                                  host TEXT,
                                                  is_secure BOOLEAN,
                                                  name TEXT,
                                                  path TEXT,
                                                  value TEXT,
                                                  same_site TEXT,
                                                  first_party_domain TEXT,
                                                  store_id TEXT,
                                                  time_stamp TIMESTAMP
);

CREATE TABLE IF NOT EXISTS navigations (
                                           id SERIAL PRIMARY KEY,
                                           incognito BOOLEAN,
                                           browser_id BIGINT NOT NULL,
                                           visit_id BIGINT NOT NULL,
                                           extension_session_uuid TEXT,
                                           process_id BIGINT,
                                           window_id BIGINT,
                                           tab_id BIGINT,
                                           tab_opener_tab_id BIGINT,
                                           frame_id BIGINT,
                                           parent_frame_id BIGINT,
                                           window_width BIGINT,
                                           window_height BIGINT,
                                           window_type TEXT,
                                           tab_width BIGINT,
                                           tab_height BIGINT,
                                           tab_cookie_store_id TEXT,
                                           uuid TEXT,
                                           url TEXT,
                                           transition_qualifiers TEXT,
                                           transition_type TEXT,
                                           before_navigate_event_ordinal BIGINT,
                                           before_navigate_time_stamp TIMESTAMP,
                                           committed_event_ordinal BIGINT,
                                           committed_time_stamp TIMESTAMP
);

CREATE TABLE IF NOT EXISTS callstacks (
                                          id SERIAL PRIMARY KEY,
                                          request_id BIGINT NOT NULL,
                                          browser_id BIGINT NOT NULL,
                                          visit_id BIGINT NOT NULL,
                                          call_stack TEXT
);

CREATE TABLE IF NOT EXISTS incomplete_visits (
                                                 visit_id BIGINT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS dns_responses (
                                             id SERIAL PRIMARY KEY,
                                             request_id BIGINT NOT NULL,
                                             browser_id BIGINT NOT NULL,
                                             visit_id BIGINT NOT NULL,
                                             hostname TEXT,
                                             addresses TEXT,
                                             used_address TEXT,
                                             canonical_name TEXT,
                                             is_TRR BOOLEAN,
                                             time_stamp TIMESTAMP NOT NULL
);