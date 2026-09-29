package picker.features.users

import org.jetbrains.exposed.v1.core.JoinType
import org.jetbrains.exposed.v1.core.SortOrder
import org.jetbrains.exposed.v1.core.and
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.core.inList
import org.jetbrains.exposed.v1.core.isNotNull
import org.jetbrains.exposed.v1.jdbc.deleteWhere
import org.jetbrains.exposed.v1.jdbc.insert
import org.jetbrains.exposed.v1.jdbc.insertIgnore
import org.jetbrains.exposed.v1.jdbc.select
import org.jetbrains.exposed.v1.jdbc.selectAll
import org.jetbrains.exposed.v1.jdbc.update
import org.jetbrains.exposed.v1.jdbc.upsert
import picker.infra.db.AnimeTable
import picker.infra.db.AppUserTable
import picker.infra.db.Db
import picker.infra.db.UserAnimeTable
import picker.infra.db.UserFeedbackTable
import picker.infra.db.tx
import java.time.OffsetDateTime
import java.util.UUID

data class UserStats(val listSize: Long, val ratings: Long)

data class Profile(
    val displayName: String?,
    val locale: String,
    val showAdult: Boolean,
    val onboardingCompleted: Boolean,
    val likedGenres: List<String>,
    val dislikedGenres: List<String>,
)

/** A PATCH of the profile: `null` means "leave as is"; [displayName] can be set to null via [clearDisplayName]. */
data class ProfileChange(
    val displayName: String? = null,
    val clearDisplayName: Boolean = false,
    val showAdult: Boolean? = null,
    val locale: String? = null,
)

enum class ListSort { ADDED, SCORE, LENGTH }

/** The signed-in user's list entry for an anime. */
data class UserEntry(val status: String, val score: Int?)

/** List statuses that count as "watched" for `hideWatched`; `planned` stays pickable. */
val WATCHED_STATUSES = listOf("watching", "completed", "dropped")

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

    suspend fun showAdult(id: UUID): Boolean =
        db.tx {
            AppUserTable
                .selectAll()
                .where { AppUserTable.id eq id }
                .limit(1)
                .map { it[AppUserTable.showAdult] }
                .firstOrNull() ?: false
        }

    /** The user's entries for [animeIds] (for `userStatus` / `userScore` on cards). */
    suspend fun entries(id: UUID, animeIds: Collection<Long>): Map<Long, UserEntry> {
        if (animeIds.isEmpty()) return emptyMap()
        return db.tx {
            UserAnimeTable
                .selectAll()
                .where { (UserAnimeTable.userId eq id) and (UserAnimeTable.animeId inList animeIds) }
                .associate {
                    it[UserAnimeTable.animeId] to
                        UserEntry(it[UserAnimeTable.status], it[UserAnimeTable.score]?.toInt())
                }
        }
    }

    /** Watched (see [WATCHED_STATUSES]) plus `not_interested`: the source of `user:{id}:excluded`. */
    suspend fun excludedIds(id: UUID): Set<Long> =
        db.tx {
            val watched =
                UserAnimeTable
                    .selectAll()
                    .where { (UserAnimeTable.userId eq id) and (UserAnimeTable.status inList WATCHED_STATUSES) }
                    .map { it[UserAnimeTable.animeId] }
            val notInterested =
                UserFeedbackTable
                    .selectAll()
                    .where { (UserFeedbackTable.userId eq id) and (UserFeedbackTable.kind eq "not_interested") }
                    .map { it[UserFeedbackTable.animeId] }
            (watched + notInterested).toSet()
        }

    suspend fun plannedIds(id: UUID): Set<Long> =
        db.tx {
            UserAnimeTable
                .selectAll()
                .where { (UserAnimeTable.userId eq id) and (UserAnimeTable.status eq "planned") }
                .map { it[UserAnimeTable.animeId] }
                .toSet()
        }

    suspend fun profile(id: UUID): Profile? =
        db.tx {
            AppUserTable
                .selectAll()
                .where { AppUserTable.id eq id }
                .limit(1)
                .map {
                    Profile(
                        it[AppUserTable.displayName],
                        it[AppUserTable.locale],
                        it[AppUserTable.showAdult],
                        it[AppUserTable.onboardingCompletedAt] != null,
                        it[AppUserTable.likedGenres],
                        it[AppUserTable.dislikedGenres],
                    )
                }.firstOrNull()
        }

    suspend fun updateProfile(id: UUID, change: ProfileChange) =
        db.tx {
            AppUserTable.update({ AppUserTable.id eq id }) {
                if (change.clearDisplayName) it[displayName] = null
                change.displayName?.let { name -> it[displayName] = name }
                change.showAdult?.let { value -> it[showAdult] = value }
                change.locale?.let { value -> it[locale] = value }
            }
        }

    /** Favorites become `completed` with score 9; preferences are replaced. One transaction. */
    suspend fun completeOnboarding(id: UUID, favorites: List<Long>, liked: List<String>, disliked: List<String>) =
        db.tx {
            favorites.forEach { animeId -> upsertEntry(id, animeId, "completed", FAVORITE_SCORE) }
            AppUserTable.update({ AppUserTable.id eq id }) {
                it[likedGenres] = liked
                it[dislikedGenres] = disliked
                it[onboardingCompletedAt] = OffsetDateTime.now()
            }
        }

    suspend fun setEntry(id: UUID, animeId: Long, status: String, score: Int?) =
        db.tx { upsertEntry(id, animeId, status, score) }

    private fun upsertEntry(id: UUID, animeId: Long, status: String, score: Int?) {
        UserAnimeTable.upsert {
            it[userId] = id
            it[UserAnimeTable.animeId] = animeId
            it[UserAnimeTable.status] = status
            it[UserAnimeTable.score] = score?.toShort()
            it[updatedAt] = OffsetDateTime.now()
        }
    }

    suspend fun deleteEntry(id: UUID, animeId: Long): Boolean =
        db.tx { UserAnimeTable.deleteWhere { (userId eq id) and (UserAnimeTable.animeId eq animeId) } > 0 }

    suspend fun addFeedback(id: UUID, animeId: Long, kind: String) =
        db.tx {
            UserFeedbackTable.insert {
                it[userId] = id
                it[UserFeedbackTable.animeId] = animeId
                it[UserFeedbackTable.kind] = kind
            }
        }

    /** Whether [animeId] belongs in `user:{id}:excluded`: watched, or marked not interested. */
    suspend fun isExcluded(id: UUID, animeId: Long): Boolean =
        db.tx {
            val watched =
                UserAnimeTable
                    .selectAll()
                    .where {
                        (UserAnimeTable.userId eq id) and (UserAnimeTable.animeId eq animeId) and
                            (UserAnimeTable.status inList WATCHED_STATUSES)
                    }.any()
            watched ||
                UserFeedbackTable
                    .selectAll()
                    .where {
                        (UserFeedbackTable.userId eq id) and (UserFeedbackTable.animeId eq animeId) and
                            (UserFeedbackTable.kind eq "not_interested")
                    }.any()
        }

    /** One page of the user's list as anime IDs, plus the total. */
    suspend fun list(id: UUID, status: String?, sort: ListSort, page: Int, size: Int): Pair<List<Long>, Long> =
        db.tx {
            val query =
                UserAnimeTable
                    .join(AnimeTable, JoinType.INNER, UserAnimeTable.animeId, AnimeTable.id)
                    .select(UserAnimeTable.animeId)
                    .where {
                        (UserAnimeTable.userId eq id).let { base ->
                            status?.let { base and (UserAnimeTable.status eq it) }
                                ?: base
                        }
                    }
            val total = query.count()
            val order =
                when (sort) {
                    ListSort.ADDED -> arrayOf(UserAnimeTable.createdAt to SortOrder.DESC)
                    ListSort.SCORE ->
                        arrayOf(
                            UserAnimeTable.score to SortOrder.DESC_NULLS_LAST,
                            UserAnimeTable.updatedAt to SortOrder.DESC,
                        )
                    ListSort.LENGTH ->
                        arrayOf(
                            AnimeTable.episodes to SortOrder.ASC_NULLS_LAST,
                            UserAnimeTable.createdAt to SortOrder.DESC,
                        )
                }
            val ids =
                query
                    .orderBy(*order, UserAnimeTable.animeId to SortOrder.ASC)
                    .limit(size)
                    .offset(((page - 1) * size).toLong())
                    .map { it[UserAnimeTable.animeId] }
            ids to total
        }

    companion object {
        const val FAVORITE_SCORE = 9
    }
}
