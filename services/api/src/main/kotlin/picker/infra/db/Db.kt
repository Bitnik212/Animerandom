package picker.infra.db

import com.zaxxer.hikari.HikariConfig
import com.zaxxer.hikari.HikariDataSource
import org.flywaydb.core.Flyway
import org.jetbrains.exposed.v1.jdbc.Database
import picker.config.PostgresConfig
import javax.sql.DataSource

/** Hikari pool, Flyway for schema `app`, and the Exposed handle. */
class Db(val dataSource: HikariDataSource) : AutoCloseable {
    val database: Database = Database.connect(dataSource)

    override fun close() = dataSource.close()

    companion object {
        fun connect(config: PostgresConfig, poolSize: Int = 10): Db {
            val hikari =
                HikariConfig().apply {
                    jdbcUrl = config.jdbcUrl
                    username = config.user
                    password = config.password
                    maximumPoolSize = poolSize
                    poolName = "api"
                    isAutoCommit = false
                    transactionIsolation = "TRANSACTION_READ_COMMITTED"
                }
            return Db(HikariDataSource(hikari))
        }

        /**
         * Migrates only schema `app`, with its history table in `app`. In the real stack
         * infra/postgres/init creates the schema (owned by api_svc); createSchemas only
         * matters for fresh development and test databases.
         */
        fun migrate(dataSource: DataSource) {
            Flyway
                .configure()
                .dataSource(dataSource)
                .schemas("app")
                .defaultSchema("app")
                .createSchemas(true)
                .locations("classpath:db/migration")
                .load()
                .migrate()
        }
    }
}
