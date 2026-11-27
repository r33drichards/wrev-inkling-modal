//! Minimal agent engine and opt-in lifecycle/durability extension points.
pub mod agent;
pub mod durable;

#[cfg(feature = "mysql-backend")]
pub mod durable_mysql;

#[cfg(feature = "quickjs-tools")]
pub mod quickjs_tools;

#[cfg(feature = "sqlite-backend")]
pub mod durable_sqlite;
