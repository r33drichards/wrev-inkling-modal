//! One SQLite database per review session. The host owns scheduling and only
//! starts one worker for a session at a time.
use crate::{
    agent::Checkpoint,
    durable::{DurableBackend, SessionStore},
};
use anyhow::{Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use std::path::PathBuf;

pub struct SqliteBackend {
    directory: PathBuf,
}

impl SqliteBackend {
    pub fn new(directory: PathBuf) -> Self {
        Self { directory }
    }
}

struct Store(Connection);

impl SessionStore for Store {
    fn load(&self, request_id: &str) -> Result<Option<Checkpoint>> {
        let result: Option<Vec<u8>> = self
            .0
            .query_row(
                "SELECT checkpoint FROM checkpoints WHERE request_id = ?1",
                [request_id],
                |row| row.get(0),
            )
            .optional()?;
        Ok(result
            .map(|bytes| serde_json::from_slice(&bytes))
            .transpose()?)
    }

    fn latest(&self) -> Result<Option<Checkpoint>> {
        let result: Option<Vec<u8>> = self
            .0
            .query_row(
                "SELECT checkpoint FROM checkpoints ORDER BY rowid DESC LIMIT 1",
                [],
                |row| row.get(0),
            )
            .optional()?;
        Ok(result
            .map(|bytes| serde_json::from_slice(&bytes))
            .transpose()?)
    }

    fn save(&mut self, state: &Checkpoint) -> Result<()> {
        self.0.execute(
            "INSERT INTO checkpoints (request_id, checkpoint) VALUES (?1, ?2)
            ON CONFLICT(request_id) DO UPDATE SET checkpoint = excluded.checkpoint",
            params![state.request_id, serde_json::to_vec(state)?],
        )?;
        Ok(())
    }
}

impl DurableBackend for SqliteBackend {
    fn with_session<T>(
        &self,
        id: &str,
        run: impl FnOnce(&mut dyn SessionStore) -> Result<T>,
    ) -> Result<T> {
        let mut connection = Connection::open(self.directory.join("review.sqlite"))?;
        connection.execute_batch(
            "PRAGMA synchronous = FULL;
             CREATE TABLE IF NOT EXISTS session (id INTEGER PRIMARY KEY CHECK(id = 1), identity TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS checkpoints (request_id TEXT UNIQUE NOT NULL, checkpoint BLOB NOT NULL);",
        )?;
        let transaction = connection.transaction()?;
        transaction.execute(
            "INSERT OR IGNORE INTO session (id, identity) VALUES (1, ?1)",
            [id],
        )?;
        let stored: String =
            transaction.query_row("SELECT identity FROM session WHERE id = 1", [], |row| {
                row.get(0)
            })?;
        ensure!(stored == id, "Review session identity changed");
        transaction.commit()?;
        run(&mut Store(connection))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reopens_the_saved_checkpoint_and_preserves_session_identity() -> Result<()> {
        #[cfg(not(target_os = "wasi"))]
        let root = std::env::temp_dir();
        #[cfg(target_os = "wasi")]
        let root = PathBuf::from("/tmp");
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        let dir = root.join(format!("wrev-sqlite-test-{nonce}"));
        std::fs::create_dir(&dir)?;
        let backend = SqliteBackend::new(dir.clone());
        let checkpoint = Checkpoint::new("review", "input", vec![], usize::MAX);
        let result = (|| {
            backend.with_session("pinned-review", |store| store.save(&checkpoint))?;
            backend.with_session("pinned-review", |store| {
                let loaded = store.load("review")?.expect("stored checkpoint");
                assert_eq!(loaded.messages, checkpoint.messages);
                Ok(())
            })?;
            assert!(backend.with_session("changed-commits", |_| Ok(())).is_err());
            Ok(())
        })();
        std::fs::remove_dir_all(dir)?;
        result
    }
}
