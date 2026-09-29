package picker.features.anime

import io.lettuce.core.ExperimentalLettuceCoroutinesApi
import io.lettuce.core.SetArgs
import kotlinx.coroutines.flow.toList
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.jsonObject
import org.slf4j.LoggerFactory
import picker.errors.Errors
import picker.features.meta.MetaService
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.i18n.pickText
import picker.infra.rec.RecEngineClient
import picker.infra.redis.Keys
import picker.infra.redis.Redis
import java.util.UUID

/**
 * Localized anime cards (README "Anime card"), cached per locale in `cache:anime:{id}:{locale}`
 * for an hour. User fields are added after the cache, so cached cards are shared by everyone.
 */
@OptIn(ExperimentalLettuceCoroutinesApi::class)
class AnimeService(
    private val repository: AnimeRepository,
    private val users: UserRepository,
    private val meta: MetaService,
    private val redis: Redis,
    private val rec: RecEngineClient,
) {
    private val log = LoggerFactory.getLogger(AnimeService::class.java)
    private val json = Json { ignoreUnknownKeys = true }

    /** Cards for [ids] in the same order; unknown IDs are dropped. Cache first, then one DB round. */
    suspend fun cardData(ids: List<Long>, locale: AppLocale): List<CardData> {
        if (ids.isEmpty()) return emptyList()
        val distinct = ids.distinct()
        val found = HashMap<Long, CardData>()
        redis.commands.mget(*distinct.map { Keys.animeCard(it, locale.key) }.toTypedArray()).toList().forEach { kv ->
            if (kv.hasValue()) {
                runCatching { json.decodeFromString(CardData.serializer(), kv.value) }
                    .onSuccess { found[it.id] = it }
                    .onFailure { log.warn("Dropping unreadable card cache {}: {}", kv.key, it.toString()) }
            }
        }
        val missing = distinct.filterNot { it in found }
        if (missing.isNotEmpty()) {
            val genreNames = meta.genreNames(locale)
            repository.rows(missing).values.forEach { row ->
                val card = localize(row, locale, genreNames)
                found[card.id] = card
                redis.commands.set(
                    Keys.animeCard(card.id, locale.key),
                    json.encodeToString(CardData.serializer(), card),
                    SetArgs.Builder.ex(CARD_TTL_SECONDS),
                )
            }
        }
        return ids.mapNotNull { found[it] }
    }

    /** Short cards with the user's list fields, keeping the order of [ids]. */
    suspend fun summaries(ids: List<Long>, locale: AppLocale, userId: UUID?): List<AnimeSummary> {
        val cards = cardData(ids, locale)
        val entries = userId?.let { users.entries(it, cards.map(CardData::id)) }.orEmpty()
        return cards.map { it.summary(entries[it.id]) }
    }

    /** `GET /anime/{id}`: the full card plus tags, studios, and relations, as one flat object. */
    suspend fun details(id: Long, locale: AppLocale, userId: UUID?): JsonObject {
        val showAdult = userId?.let { users.showAdult(it) } ?: false
        val card = visible(cardData(listOf(id), locale).firstOrNull(), showAdult) ?: throw Errors.animeNotFound(id)
        val extras = repository.extras(id)
        val tagNames = meta.tagNames(locale)
        val related = cardData(extras.relations.map { it.second }, locale).associateBy { it.id }
        val entries = userId?.let { users.entries(it, related.keys + id) }.orEmpty()
        val relations =
            extras.relations.mapNotNull { (kind, relatedId) ->
                related[relatedId]
                    ?.takeIf { showAdult || !it.isAdult }
                    ?.let { Relation(kind, it.summary(entries[relatedId])) }
            }
        val base = apiJson.encodeToJsonElement(card.full(entries[id])).jsonObject
        return JsonObject(
            base +
                mapOf(
                    "tags" to
                        apiJson.encodeToJsonElement(
                            extras.tags.map { TagRef(it.slug, tagNames[it.slug] ?: it.slug, it.rank, it.spoiler) },
                        ),
                    "studios" to
                        apiJson.encodeToJsonElement(extras.studios.map { (name, main) -> StudioRef(name, main) }),
                    "relations" to apiJson.encodeToJsonElement(relations),
                ),
        )
    }

    /**
     * "More like this" from the rec engine. Adult titles only for a signed-in user with
     * `showAdult`. If the engine is slow or down the list is empty rather than an error.
     */
    suspend fun similar(id: Long, limit: Int, locale: AppLocale, userId: UUID?): SummaryList {
        val showAdult = userId?.let { users.showAdult(it) } ?: false
        visible(repository.rows(listOf(id))[id], showAdult) { it.isAdult } ?: throw Errors.animeNotFound(id)
        val ids = rec.similar(id, limit, includeAdult = showAdult) ?: return SummaryList(emptyList())
        return SummaryList(summaries(ids, locale, userId))
    }

    /** Adult titles don't exist for users who haven't opted in, not even by ID. */
    private fun visible(card: CardData?, showAdult: Boolean): CardData? = visible(card, showAdult) { it.isAdult }

    private fun <T : Any> visible(item: T?, showAdult: Boolean, isAdult: (T) -> Boolean): T? =
        item?.takeIf { showAdult || !isAdult(it) }

    private fun localize(row: AnimeRow, locale: AppLocale, genreNames: Map<String, String>): CardData {
        val titles = row.texts.mapValues { it.value.title }
        val title = pickText(locale.titleChain, titles) ?: pickText(AppLocale.entries, titles)
        val synopsis = pickText(locale.synopsisChain, row.texts.mapValues { it.value.synopsis })
        return CardData(
            id = row.id,
            title = title?.first ?: "#${row.id}",
            titleLocale = title?.second?.tag,
            titleNative = titles[AppLocale.JA]?.takeIf { it.isNotBlank() },
            titleRomaji = titles[AppLocale.JA_LATN]?.takeIf { it.isNotBlank() },
            synopsis = synopsis?.first,
            synopsisLocale = synopsis?.second?.tag,
            synopsisMachine = synopsis?.let { row.texts[it.second]?.synopsisMachine },
            format = row.format,
            status = row.status,
            seasonYear = row.seasonYear,
            episodes = row.episodes,
            score = row.score,
            genres = row.genres.map { GenreRef(it, genreNames[it] ?: it) },
            coverUrl = row.coverUrl,
            isAdult = row.isAdult,
        )
    }

    companion object {
        const val CARD_TTL_SECONDS = 3600L
        private val apiJson = Json { encodeDefaults = true }
    }
}
