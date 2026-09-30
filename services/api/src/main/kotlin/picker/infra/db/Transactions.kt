package picker.infra.db

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.jetbrains.exposed.v1.jdbc.JdbcTransaction
import org.jetbrains.exposed.v1.jdbc.transactions.transaction

/** Run a blocking Exposed transaction on the IO dispatcher. */
suspend fun <T> Db.tx(block: JdbcTransaction.() -> T): T =
    withContext(Dispatchers.IO) { transaction(database) { block() } }
