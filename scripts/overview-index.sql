-- Run with psql in autocommit mode (not inside BEGIN / a migration transaction).
-- Already applied manually in the development database on 2026-10-06.
-- Check the existing index definition if this name already exists.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_note_directory_case_id
    ON public.note_directory (case_id);
ANALYZE public.note_directory;
