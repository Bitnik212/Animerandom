package picker.features.recommendations

import org.jetbrains.exposed.v1.core.JoinType
import org.jetbrains.exposed.v1.core.SortOrder
import org.jetbrains.exposed.v1.core.and
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.core.inList
import org.jetbrains.exposed.v1.core.inSubQuery
import org.jetbrains.exposed.v1.core.neq
import org.jetbrains.exposed.v1.core.notInSubQuery
import org.jetbrains.exposed.v1.jdbc.select
import picker.infra.db.AnimeGenreTable
import picker.infra.db.AnimeTable
import picker.infra.db.Db
import picker.infra.db.GenreTable
import picker.infra.db.UserAnimeTable
import picker.infra.db.UserFeedbackTable
import picker.infra.db.tx
import java.util.UUID

/** The fallback feed when the rec engine can't answer: popularity, read from `catalog.*`. */
class RecommendationRepository(private val db: Db) {
    /**
     * Most popular anime the user has neither listed nor rejected, in [genres] when given, never in
     * [disliked], never `REMOVED`, adult only when [showAdult].
     */
    suspend fun popular(
        userId: UUID,
        genres: List<String>,
        disliked: List<String>,
        showAdult: Boolean,
        limit: Int,
    ): List<Long> =
        db.tx {
            fun withGenres(slugs: List<String>) =
                AnimeGenreTable
                    .join(GenreTable, JoinType.INNER, AnimeGenreTable.genreId, GenreTable.id)
                    .select(AnimeGenreTable.animeId)
                    .where { GenreTable.slug inList slugs }
            val listed = UserAnimeTable.select(UserAnimeTable.animeId).where { UserAnimeTable.userId eq userId }
            val rejected =
                UserFeedbackTable
                    .select(UserFeedbackTable.animeId)
                    .where { (UserFeedbackTable.userId eq userId) and (UserFeedbackTable.kind eq "not_interested") }
            var condition =
                (AnimeTable.status neq "REMOVED") and
                    (AnimeTable.id notInSubQuery listed) and
                    (AnimeTable.id notInSubQuery rejected)
            if (!showAdult) condition = condition and (AnimeTable.isAdult eq false)
            if (genres.isNotEmpty()) condition = condition and (AnimeTable.id inSubQuery withGenres(genres))
            if (disliked.isNotEmpty()) condition = condition and (AnimeTable.id notInSubQuery withGenres(disliked))
            AnimeTable
                .select(AnimeTable.id)
                .where { condition }
                .orderBy(AnimeTable.popularity to SortOrder.DESC_NULLS_LAST, AnimeTable.id to SortOrder.ASC)
                .limit(limit)
                .map { it[AnimeTable.id] }
        }
}
