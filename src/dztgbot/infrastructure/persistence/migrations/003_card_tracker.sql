CREATE TABLE IF NOT EXISTS card_message_tracker (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    issue_key TEXT NOT NULL CHECK (length(issue_key) >= 2),
    owner_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
) STRICT;

CREATE INDEX idx_card_tracker_issue
    ON card_message_tracker(issue_key);
