package picker.features.users

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import picker.auth.UserPrincipal
import picker.errors.Errors
import picker.features.anime.AnimeRepository
import picker.features.anime.AnimeService
import picker.features.anime.AnimeSummary
import picker.features.auth.AccountService
import picker.features.meta.MetaService
import picker.i18n.AppLocale
import java.util.UUID

@Serializable
data class ProfileView(
    val id: String,
    val email: String?,
    val displayName: String?,
    val roles: List<String>,
    val locale: String,
    val showAdult: Boolean,
    val onboardingCompleted: Boolean,
    val likedGenres: List<String>,
    val dislikedGenres: List<String>,
)

@Serializable
data class OnboardingRequest(
    val favorites: List<Long> = emptyList(),
    val likedGenres: List<String> = emptyList(),
    val dislikedGenres: List<String> = emptyList(),
)

@Serializable
data class EntryRequest(val status: String, val score: Int? = null)

@Serializable
data class FeedbackRequest(val animeId: Long, val kind: String)

@Serializable
data class UserList(val items: List<AnimeSummary>, val total: Long, val page: Int, val size: Int)

/**
 * The current user's profile, onboarding, list and feedback (README "Current user"). Every list
 * or feedback change keeps `user:{id}:excluded` in step, so random picks see it at once.
 */
class UsersService(
    private val users: UserRepository,
    private val exclusions: Exclusions,
    private val animeRepository: AnimeRepository,
    private val anime: AnimeService,
    private val meta: MetaService,
    private val account: AccountService,
) {
    suspend fun profile(principal: UserPrincipal): ProfileView {
        val profile = users.profile(principal.id) ?: throw Errors.userNotFound()
        return ProfileView(
            id = principal.id.toString(),
            email = principal.email,
            displayName = profile.displayName,
            roles = principal.roles.filter { it == "user" || it == UserPrincipal.ADMIN_ROLE }.sorted(),
            locale = (AppLocale.fromKey(profile.locale) ?: AppLocale.EN).tag,
            showAdult = profile.showAdult,
            onboardingCompleted = profile.onboardingCompleted,
            likedGenres = profile.likedGenres,
            dislikedGenres = profile.dislikedGenres,
        )
    }

    /**
     * Applies the fields present in [patch]. A new locale goes to Keycloak first (for emails),
     * so a Keycloak failure is a 502 with nothing changed and the client can simply retry.
     */
    suspend fun update(principal: UserPrincipal, patch: JsonObject): ProfileView {
        val unknown = patch.keys - PATCHABLE
        if (unknown.isNotEmpty()) throw Errors.validation("Unknown fields: ${unknown.sorted().joinToString()}")
        val (displayName, clearDisplayName) = displayNameField(patch["displayName"])
        val locale = patch["locale"]?.let(::localeField)
        val change = ProfileChange(displayName, clearDisplayName, patch["showAdult"]?.let(::booleanField), locale?.key)
        val current = users.profile(principal.id) ?: throw Errors.userNotFound()
        if (locale != null && locale.key != current.locale) account.syncLocale(principal.id, locale)
        users.updateProfile(principal.id, change)
        return profile(principal)
    }

    /** New value (or null) and whether to clear it: JSON `null` or a blank string clears. */
    private fun displayNameField(field: JsonElement?): Pair<String?, Boolean> =
        when {
            field == null -> null to false
            field is JsonNull -> null to true
            field is JsonPrimitive && field.isString -> displayNameOf(field.content).let { it to (it == null) }
            else -> throw Errors.validation("displayName must be a string")
        }

    private fun booleanField(field: JsonElement): Boolean =
        (field as? JsonPrimitive)?.takeIf { !it.isString }?.booleanOrNull
            ?: throw Errors.validation("showAdult must be a boolean")

    private fun localeField(field: JsonElement): AppLocale {
        val tag =
            (field as? JsonPrimitive)?.takeIf { it.isString }?.content
                ?: throw Errors.validation("locale must be a string")
        return AppLocale.fromTag(tag) ?: throw Errors.unsupportedLocale(tag)
    }

    suspend fun onboard(userId: UUID, request: OnboardingRequest) {
        val favorites = favoritesOf(request.favorites)
        val genres = meta.genreNames(AppLocale.EN).keys
        val liked = genreSlugs("likedGenres", request.likedGenres, genres)
        val disliked = genreSlugs("dislikedGenres", request.dislikedGenres, genres)
        if (liked
                .intersect(
                    disliked.toSet(),
                ).isNotEmpty()
        ) {
            throw Errors.validation("A genre can't be both liked and disliked")
        }
        users.completeOnboarding(userId, favorites, liked, disliked)
        favorites.forEach { exclusions.add(userId, it) }
    }

    private suspend fun favoritesOf(ids: List<Long>): List<Long> {
        if (ids.size > MAX_FAVORITES) throw Errors.validation("At most $MAX_FAVORITES favorites")
        val favorites = ids.distinct()
        (favorites - animeRepository.rows(favorites).keys).firstOrNull()?.let { throw Errors.animeNotFound(it) }
        return favorites
    }

    suspend fun list(userId: UUID, status: String?, sort: ListSort, page: Int, size: Int, locale: AppLocale): UserList {
        status?.let(::checkStatus)
        val (ids, total) = users.list(userId, status, sort, page, size)
        return UserList(anime.summaries(ids, locale, userId), total, page, size)
    }

    suspend fun setEntry(userId: UUID, animeId: Long, request: EntryRequest, locale: AppLocale): AnimeSummary {
        checkScore(request.status, request.score)
        val card = anime.summaries(listOf(animeId), locale, null).firstOrNull() ?: throw Errors.animeNotFound(animeId)
        users.setEntry(userId, animeId, request.status, request.score)
        syncExclusion(userId, animeId)
        return card.copy(userStatus = request.status, userScore = request.score)
    }

    suspend fun removeEntry(userId: UUID, animeId: Long) {
        users.deleteEntry(userId, animeId)
        syncExclusion(userId, animeId)
    }

    suspend fun feedback(userId: UUID, request: FeedbackRequest) {
        if (request.kind !in
            FEEDBACK_KINDS
        ) {
            throw Errors.validation("kind must be one of ${FEEDBACK_KINDS.joinToString()}")
        }
        if (animeRepository.rows(listOf(request.animeId)).isEmpty()) throw Errors.animeNotFound(request.animeId)
        users.addFeedback(userId, request.animeId, request.kind)
        if (request.kind == NOT_INTERESTED) exclusions.add(userId, request.animeId)
    }

    /** Adds or removes one ID so the cached set matches Postgres (a title can be excluded for two reasons). */
    private suspend fun syncExclusion(userId: UUID, animeId: Long) {
        if (users.isExcluded(userId, animeId)) exclusions.add(userId, animeId) else exclusions.remove(userId, animeId)
    }

    private fun displayNameOf(value: String): String? =
        value.trim().takeIf { it.isNotEmpty() }?.also {
            if (it.length > MAX_DISPLAY_NAME) throw Errors.validation("displayName is longer than $MAX_DISPLAY_NAME")
        }

    private fun genreSlugs(field: String, values: List<String>, known: Set<String>): List<String> {
        val slugs = values.map { it.trim().lowercase() }.distinct()
        slugs.firstOrNull { it !in known }?.let { throw Errors.validation("Unknown genre '$it' in $field") }
        return slugs
    }

    /** A score is an opinion of something seen; on `planned` the rec engine would read it as a like. */
    private fun checkScore(status: String, score: Int?) {
        checkStatus(status)
        if (score == null) return
        if (status == "planned") throw Errors.validation("A planned title can't have a score")
        if (score !in 1..MAX_SCORE) throw Errors.validation("score must be between 1 and $MAX_SCORE")
    }

    private fun checkStatus(status: String) {
        if (status !in STATUSES) throw Errors.validation("status must be one of ${STATUSES.joinToString()}")
    }

    companion object {
        val STATUSES = listOf("planned", "watching", "completed", "dropped")
        val FEEDBACK_KINDS = listOf(NOT_INTERESTED, "skipped")
        private const val NOT_INTERESTED = "not_interested"
        private val PATCHABLE = setOf("displayName", "showAdult", "locale")
        const val MAX_FAVORITES = 50
        const val MAX_SCORE = 10
        const val MAX_DISPLAY_NAME = 80
    }
}
