package picker.features.meta

import kotlinx.serialization.Serializable
import picker.i18n.AppLocale
import picker.i18n.pickText
import java.time.Clock
import java.time.Duration
import java.time.Instant

@Serializable
data class GenreDto(val slug: String, val name: String)

@Serializable
data class TagDto(val slug: String, val name: String, val category: String?, val spoiler: Boolean)

/**
 * Genre and tag names, localized with the title fallback chain. The vocabulary changes
 * only when the ingest worker syncs its YAML, so it's cached in memory for 10 minutes.
 */
class MetaService(
    private val repository: MetaRepository,
    private val clock: Clock = Clock.systemUTC(),
    private val ttl: Duration = Duration.ofMinutes(10),
) {
    private class Cached(val genres: List<VocabTerm>, val tags: List<VocabTerm>, val loadedAt: Instant)

    @Volatile
    private var cache: Cached? = null

    private suspend fun vocab(): Cached {
        cache?.takeIf { Duration.between(it.loadedAt, clock.instant()) < ttl }?.let { return it }
        return Cached(repository.genres(), repository.tags(), clock.instant()).also { cache = it }
    }

    fun name(term: VocabTerm, locale: AppLocale): String = pickText(locale.titleChain, term.names)?.first ?: term.slug

    suspend fun genres(locale: AppLocale): List<GenreDto> = vocab().genres.map { GenreDto(it.slug, name(it, locale)) }

    /** Genre slug → localized name, for cards and facets. */
    suspend fun genreNames(locale: AppLocale): Map<String, String> =
        vocab().genres.associate {
            it.slug to
                name(it, locale)
        }

    suspend fun tagNames(locale: AppLocale): Map<String, String> =
        vocab().tags.associate {
            it.slug to
                name(
                    it,
                    locale,
                )
        }

    /** Tags whose localized name or slug starts with [prefix] (case-insensitive). */
    suspend fun tags(locale: AppLocale, prefix: String?): List<TagDto> {
        val needle = prefix?.trim()?.lowercase().orEmpty()
        return vocab()
            .tags
            .map { TagDto(it.slug, name(it, locale), it.category, it.isSpoiler) }
            .filter { needle.isEmpty() || it.name.lowercase().startsWith(needle) || it.slug.startsWith(needle) }
            .sortedBy { it.name.lowercase() }
    }
}
