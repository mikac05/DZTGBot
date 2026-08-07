CREATE TABLE workflows (
    draft_id TEXT PRIMARY KEY NOT NULL,
    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
    chat_id INTEGER NOT NULL CHECK (chat_id <> 0),
    message_thread_id INTEGER,
    state TEXT NOT NULL CHECK (
        state IN (
            'collecting',
            'analyzing',
            'analysis_failed',
            'review',
            'editing',
            'submitting',
            'submission_retryable',
            'submission_unknown',
            'created',
            'attaching',
            'attachment_partial',
            'complete',
            'update_review',
            'updating',
            'update_retryable',
            'update_unknown',
            'cancelled',
            'expired',
            'abandoned_unknown'
        )
    ),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    template_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    last_error TEXT
) STRICT;

CREATE TABLE source_messages (
    draft_id TEXT NOT NULL REFERENCES workflows(draft_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    message_id INTEGER NOT NULL CHECK (message_id > 0),
    chat_id INTEGER NOT NULL CHECK (chat_id <> 0),
    sender_id INTEGER NOT NULL CHECK (sender_id > 0),
    text_content TEXT NOT NULL,
    media_kind TEXT NOT NULL CHECK (
        media_kind IN ('text', 'photo', 'document', 'video', 'voice')
    ),
    received_at TEXT NOT NULL,
    PRIMARY KEY (draft_id, position),
    UNIQUE (draft_id, chat_id, message_id)
) STRICT;

CREATE TABLE attachments (
    draft_id TEXT NOT NULL REFERENCES workflows(draft_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    file_id TEXT NOT NULL CHECK (length(file_id) > 0),
    file_unique_id TEXT NOT NULL CHECK (length(file_unique_id) > 0),
    media_kind TEXT NOT NULL CHECK (
        media_kind IN ('text', 'photo', 'document', 'video', 'voice')
    ),
    file_name TEXT,
    file_size INTEGER CHECK (file_size IS NULL OR file_size >= 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'uploading', 'uploaded', 'failed', 'skipped')
    ),
    uploaded_attachment_id TEXT,
    last_error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (draft_id, position),
    UNIQUE (draft_id, file_unique_id)
) STRICT;

CREATE TABLE published_issues (
    draft_id TEXT PRIMARY KEY NOT NULL
        REFERENCES workflows(draft_id) ON DELETE CASCADE,
    issue_key TEXT NOT NULL UNIQUE CHECK (length(issue_key) > 0),
    issue_id TEXT NOT NULL CHECK (length(issue_id) > 0),
    issue_url TEXT NOT NULL CHECK (length(issue_url) > 0),
    published_at TEXT NOT NULL
) STRICT;

CREATE TABLE callback_tokens (
    token_hash TEXT PRIMARY KEY NOT NULL CHECK (
        length(token_hash) = 64
        AND token_hash NOT GLOB '*[^0-9a-f]*'
    ),
    draft_id TEXT NOT NULL REFERENCES workflows(draft_id) ON DELETE CASCADE,
    owner_user_id INTEGER NOT NULL CHECK (owner_user_id > 0),
    chat_id INTEGER NOT NULL CHECK (chat_id <> 0),
    message_thread_id INTEGER,
    preview_message_id INTEGER CHECK (
        preview_message_id IS NULL OR preview_message_id > 0
    ),
    expected_revision INTEGER NOT NULL CHECK (expected_revision >= 1),
    expected_state TEXT NOT NULL CHECK (
        expected_state IN (
            'collecting',
            'analyzing',
            'analysis_failed',
            'review',
            'editing',
            'submitting',
            'submission_retryable',
            'submission_unknown',
            'created',
            'attaching',
            'attachment_partial',
            'complete',
            'update_review',
            'updating',
            'update_retryable',
            'update_unknown',
            'cancelled',
            'expired',
            'abandoned_unknown'
        )
    ),
    action TEXT NOT NULL CHECK (
        action IN ('cfm', 'edt', 'cnl', 'cpl', 'cps', 'edp', 'ttyp', 'tpri', 'rty', 'rcn')
    ),
    expires_at TEXT NOT NULL,
    one_shot INTEGER NOT NULL CHECK (one_shot IN (0, 1)),
    consumed_at TEXT
) STRICT;

CREATE TABLE submission_attempts (
    attempt_id TEXT PRIMARY KEY NOT NULL,
    draft_id TEXT NOT NULL REFERENCES workflows(draft_id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL CHECK (length(request_hash) > 0),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'success', 'failed', 'unknown')
    ),
    error_summary TEXT,
    UNIQUE (draft_id, attempt_number)
) STRICT;
