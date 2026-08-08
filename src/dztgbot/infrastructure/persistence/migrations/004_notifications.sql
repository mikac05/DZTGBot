CREATE TABLE user_notification_tracker (
    user_id INTEGER NOT NULL,
    issue_key TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    PRIMARY KEY (user_id, issue_key)
) STRICT;
