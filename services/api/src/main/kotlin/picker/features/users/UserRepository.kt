package picker.features.users

import org.jetbrains.exposed.v1.core.and
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.core.isNotNull
import org.jetbrains.exposed.v1.jdbc.deleteWhere
import org.jetbrains.exposed.v1.jdbc.insert
import org.jetbrains.exposed.v1.jdbc.insertIgnore
import org.jetbrains.exposed.v1.jdbc.selectAll
import picker.infra.db.AppUserTable
import picker.infra.db.Db
import picker.infra.db.UserAnimeTable
import picker.infra.db.UserFeedbackTable
import picker.infra.db.tx
import java.util.UUID

data class UserStats(val listSize: Long, val ratings: Long)

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

    suspend fun create(id: UUID, displayName: String?, locale: String) =
        db.tx {
            AppUserTable.insert {
                it[AppUserTable.id] = id
                it[AppUserTable.displayName] = displayName
                it[AppUserTable.locale] = locale
            }
        }

    /** One transaction: feedback, list, then the user row. Idempotent. */
    suspend fun deleteAll(id: UUID) =
        db.tx {
            UserFeedbackTable.deleteWhere { UserFeedbackTable.userId eq id }
            UserAnimeTable.deleteWhere { UserAnimeTable.userId eq id }
            AppUserTable.deleteWhere { AppUserTable.id eq id }
        }

    suspend fun exists(id: UUID): Boolean =
        db.tx {
            AppUserTable
                .selectAll()
                .where { AppUserTable.id eq id }
                .limit(1)
                .any()
        }

    suspend fun stats(id: UUID): UserStats =
        db.tx {
            val list = UserAnimeTable.selectAll().where { UserAnimeTable.userId eq id }.count()
            val rated =
                UserAnimeTable
                    .selectAll()
                    .where { (UserAnimeTable.userId eq id) and UserAnimeTable.score.isNotNull() }
                    .count()
            UserStats(list, rated)
        }
}
