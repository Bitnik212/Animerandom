package picker.features.users

import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.jdbc.insertIgnore
import org.jetbrains.exposed.v1.jdbc.selectAll
import picker.infra.db.AppUserTable
import picker.infra.db.Db
import picker.infra.db.tx
import java.util.UUID

class UserRepository(private val db: Db) {
    /** `INSERT … ON CONFLICT DO NOTHING`: safe to call for every first request of a user. */
    suspend fun ensure(id: UUID, locale: String = "en") =
        db.tx {
            AppUserTable.insertIgnore {
                it[AppUserTable.id] = id
                it[AppUserTable.locale] = locale
            }
        }

    suspend fun locale(id: UUID): String? =
        db.tx {
            AppUserTable
                .selectAll()
                .where { AppUserTable.id eq id }
                .limit(1)
                .map { it[AppUserTable.locale] }
                .firstOrNull()
        }
}
