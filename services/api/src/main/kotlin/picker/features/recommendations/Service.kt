package picker.features.recommendations

import kotlinx.serialization.Serializable
import picker.features.anime.AnimeService
import picker.features.anime.AnimeSummary
import picker.features.meta.MetaService
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.i18n.Messages
import picker.infra.rec.RecEngineClient
import picker.infra.rec.RecReason
import java.util.UUID

@Serializable
data class Reason(val code: String, val text: String, val animeId: Long? = null, val genre: String? = null)

@Serializable
data class Recommendation(val card: AnimeSummary, val reason: Reason)

@Serializable
data class RecommendationFeed(val items: List<Recommendation>)

/**
 * The personal feed (README "Recommendations"): rec engine IDs, hydrated into cards in the
 * engine's order, each with a localized reason. If the engine fails, popular titles in the user's
 * liked genres (or overall) with reason `popular`. The client never sees the engine's errors.
 */
class RecommendationService(
    private val rec: RecEngineClient,
    private val repository: RecommendationRepository,
    private val anime: AnimeService,
    private val meta: MetaService,
    private val users: UserRepository,
) {
    suspend fun feed(userId: UUID, limit: Int, locale: AppLocale): RecommendationFeed {
        val items = rec.recommendations(userId, limit)?.map { it.animeId to it.reason } ?: fallback(userId, limit)
        val cards = anime.summaries(items.map { it.first }, locale, userId).associateBy { it.id }
        val referenced = items.mapNotNull { it.second.animeId }.filter { it !in cards }
        val titles =
            cards.mapValues { it.value.title } + anime.cardData(referenced, locale).associate { it.id to it.title }
        val genreNames = meta.genreNames(locale)
        return RecommendationFeed(
            items.mapNotNull { (id, reason) ->
                cards[id]?.let { Recommendation(it, localize(reason, locale, titles, genreNames)) }
            },
        )
    }

    private suspend fun fallback(userId: UUID, limit: Int): List<Pair<Long, RecReason>> {
        val profile = users.profile(userId)
        val liked = profile?.likedGenres.orEmpty()
        val disliked = profile?.dislikedGenres.orEmpty()
        val showAdult = profile?.showAdult ?: false
        val inGenres =
            if (liked.isEmpty()) {
                emptyList()
            } else {
                repository.popular(
                    userId,
                    liked,
                    disliked,
                    showAdult,
                    limit,
                )
            }
        val overall =
            if (inGenres.size < limit) {
                repository.popular(userId, emptyList(), disliked, showAdult, limit).filterNot { it in inGenres }
            } else {
                emptyList()
            }
        return (inGenres + overall).take(limit).map { it to RecReason(POPULAR) }
    }

    /** Reason text from `messages/`. Codes this version doesn't know read as `popular`. */
    private fun localize(
        reason: RecReason,
        locale: AppLocale,
        titles: Map<Long, String>,
        genres: Map<String, String>,
    ): Reason {
        val similarTitle = reason.animeId?.let { titles[it] }
        return when {
            reason.code == SIMILAR_TO && similarTitle != null ->
                Reason(
                    SIMILAR_TO,
                    Messages.format(locale, "reason.similar_to", mapOf("title" to similarTitle)),
                    reason.animeId,
                )
            reason.code == LIKED_BY_SIMILAR_USERS ->
                Reason(LIKED_BY_SIMILAR_USERS, Messages.format(locale, "reason.liked_by_similar_users"))
            reason.code == POPULAR_IN_GENRE && reason.genre != null ->
                Reason(
                    POPULAR_IN_GENRE,
                    Messages.format(
                        locale,
                        "reason.popular_in_genre",
                        mapOf(
                            "genre" to (genres[reason.genre] ?: reason.genre),
                        ),
                    ),
                    genre = reason.genre,
                )
            else -> Reason(POPULAR, Messages.format(locale, "reason.popular"))
        }
    }

    companion object {
        const val SIMILAR_TO = "similar_to"
        const val LIKED_BY_SIMILAR_USERS = "liked_by_similar_users"
        const val POPULAR_IN_GENRE = "popular_in_genre"
        const val POPULAR = "popular"
    }
}
