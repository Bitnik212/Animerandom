package picker.features.anime

import kotlinx.serialization.Serializable
import picker.features.users.UserEntry

@Serializable
data class GenreRef(val slug: String, val name: String)

/**
 * The localized card without user fields, as cached in `cache:anime:{id}:{locale}`. Everything a
 * client sees is derived from it: [full] for detail views, [summary] for lists.
 */
@Serializable
data class CardData(
    val id: Long,
    val title: String,
    val titleLocale: String?,
    val titleNative: String?,
    val titleRomaji: String?,
    val synopsis: String?,
    val synopsisLocale: String?,
    val synopsisMachine: Boolean?,
    val format: String?,
    val status: String?,
    val seasonYear: Int?,
    val episodes: Int?,
    val score: Double?,
    val genres: List<GenreRef>,
    val coverUrl: String?,
    val isAdult: Boolean,
) {
    fun full(user: UserEntry?) =
        AnimeCard(
            id,
            title,
            titleLocale,
            titleNative,
            titleRomaji,
            synopsis,
            synopsisLocale,
            synopsisMachine,
            format,
            status,
            seasonYear,
            episodes,
            score,
            genres,
            coverUrl,
            user?.status,
            user?.score,
        )

    fun summary(user: UserEntry?) =
        AnimeSummary(
            id,
            title,
            titleLocale,
            titleNative,
            titleRomaji,
            format,
            status,
            seasonYear,
            episodes,
            score,
            genres,
            coverUrl,
            user?.status,
            user?.score,
        )
}

/** Full card (README "Anime card"). */
@Serializable
data class AnimeCard(
    val id: Long,
    val title: String,
    val titleLocale: String?,
    val titleNative: String?,
    val titleRomaji: String?,
    val synopsis: String?,
    val synopsisLocale: String?,
    val synopsisMachine: Boolean?,
    val format: String?,
    val status: String?,
    val seasonYear: Int?,
    val episodes: Int?,
    val score: Double?,
    val genres: List<GenreRef>,
    val coverUrl: String?,
    val userStatus: String?,
    val userScore: Int?,
)

/** The shorter card list endpoints return: no synopsis. */
@Serializable
data class AnimeSummary(
    val id: Long,
    val title: String,
    val titleLocale: String?,
    val titleNative: String?,
    val titleRomaji: String?,
    val format: String?,
    val status: String?,
    val seasonYear: Int?,
    val episodes: Int?,
    val score: Double?,
    val genres: List<GenreRef>,
    val coverUrl: String?,
    val userStatus: String?,
    val userScore: Int?,
)

@Serializable
data class TagRef(val slug: String, val name: String, val rank: Int, val spoiler: Boolean)

@Serializable
data class StudioRef(val name: String, val main: Boolean)

@Serializable
data class Relation(val kind: String, val anime: AnimeSummary)

@Serializable
data class SummaryList(val items: List<AnimeSummary>)
