CREATE INDEX idx_workflows_owner_chat_state
    ON workflows(owner_id, chat_id, state);

CREATE INDEX idx_workflows_expiry
    ON workflows(expires_at, state)
    WHERE expires_at IS NOT NULL;

CREATE INDEX idx_workflows_updated_state
    ON workflows(updated_at, state);

CREATE INDEX idx_callbacks_draft_revision
    ON callback_tokens(draft_id, expected_revision);

CREATE INDEX idx_callbacks_expiry
    ON callback_tokens(expires_at)
    WHERE consumed_at IS NULL;

CREATE UNIQUE INDEX uq_callbacks_rendered_action
    ON callback_tokens(
        draft_id,
        expected_revision,
        action,
        COALESCE(preview_message_id, -1)
    );

CREATE INDEX idx_attempts_draft_latest
    ON submission_attempts(draft_id, attempt_number DESC);

CREATE UNIQUE INDEX uq_attempts_one_active_per_draft
    ON submission_attempts(draft_id)
    WHERE status = 'pending';

CREATE INDEX idx_attachments_draft_status
    ON attachments(draft_id, status, position);
