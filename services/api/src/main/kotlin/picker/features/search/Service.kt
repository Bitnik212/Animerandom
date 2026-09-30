package picker.features.search

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import picker.errors.Errors
import picker.features.anime.AnimeService
import picker.features.anime.AnimeSummary
import picker.features.anime.SummaryList
import picker.features.meta.MetaService
import picker.features.users.Exclusions
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.infra.es.Es
import picker.infra.es.Q
import java.util.UUID

enum class SearchSort { RELEVANCE, POPULARITY, SCORE, NEWEST }

/** `GET /search` parameters (README "Search"). */
data class SearchQuery(
    val q: String?,
    val filters: AnimeFilters,
    val hideWatched: Boolean,
    val sort: SearchSort,
    val page: Int,
    val size: Int,
) {
    companion object {
        const val MAX_SIZE = 50
        const val MAX_WINDOW = 10_000
        const val MAX_QUERY = 200
        private val EPISODES = 0..10_000

        fun parse(all: (String) -> List<String>): SearchQuery {
            fun one(name: String) = all(name).firstOrNull()
            val q = query(one("q"))
            val sort = sort(one("sort"), q != null)
            val page = AnimeFilters.int("page", one("page"), 1..MAX_WINDOW) ?: 1
            val size = AnimeFilters.int("size", one("size"), 1..MAX_SIZE) ?: DEFAULT_SIZE
            if (page.toLong() * size > MAX_WINDOW) throw Errors.validation("page × size must not exceed $MAX_WINDOW")
            return SearchQuery(
                q = q,
                filters =
                    AnimeFilters(
                        genres = AnimeFilters.slugs("genre", all("genre")),
                        excludeGenres = AnimeFilters.slugs("excludeGenre", all("excludeGenre")),
                        tags = AnimeFilters.slugs("tag", all("tag")),
                        excludeTags = AnimeFilters.slugs("excludeTag", all("excludeTag")),
                        formats = AnimeFilters.enums("format", all("format")),
                        statuses = AnimeFilters.enums("status", all("status")),
                        yearFrom = AnimeFilters.int("yearFrom", one("yearFrom"), AnimeFilters.YEARS),
                        yearTo = AnimeFilters.int("yearTo", one("yearTo"), AnimeFilters.YEARS),
                        minScore = AnimeFilters.score(one("minScore")),
                        episodesMin = AnimeFilters.int("episodesMin", one("episodesMin"), EPISODES),
                        episodesMax = AnimeFilters.int("episodesMax", one("episodesMax"), EPISODES),
                    ),
                hideWatched = AnimeFilters.bool("hideWatched", one("hideWatched")) ?: false,
                sort = sort,
                page = page,
                size = size,
            )
        }

        private const val DEFAULT_SIZE = 20

        private fun query(value: String?): String? {
            val q = value?.trim()?.takeIf { it.isNotEmpty() }
            if (q != null && q.length > MAX_QUERY) throw Errors.validation("q is longer than $MAX_QUERY characters")
            return q
        }

        private fun sort(value: String?, hasQuery: Boolean): SearchSort {
            if (value == null) return if (hasQuery) SearchSort.RELEVANCE else SearchSort.POPULARITY
            return SearchSort.entries.firstOrNull { it.name.equals(value, ignoreCase = true) }
                ?: throw Errors.validation("sort must be relevance, popularity, score or newest")
        }
    }
}

@Serializable
data class GenreFacet(val slug: String, val name: String, val count: Long)

@Serializable
data class ValueFacet(val value: String, val count: Long)

@Serializable
data class Facets(val genres: List<GenreFacet>, val formats: List<ValueFacet>, val decades: List<ValueFacet>)

@Serializable
data class SearchResult(val items: List<AnimeSummary>, val total: Long, val facets: Facets)

/**
 * Search, suggest and facets over the `anime` alias (README "Search"). Hits are IDs only; cards
 * come from [AnimeService] so search results look exactly like every other list.
 */
class SearchService(
    private val es: Es,
    private val anime: AnimeService,
    private val meta: MetaService,
    private val users: UserRepository,
    private val exclusions: Exclusions,
) {
    suspend fun search(query: SearchQuery, locale: AppLocale, userId: UUID?): SearchResult {
        val f = query.filters
        val showAdult = userId?.let { users.showAdult(it) } ?: false
        val excluded = if (query.hideWatched && userId != null) exclusions.ids(userId) else emptySet()
        val base = f.baseClauses(showAdult, excluded, onlyIds = null)
        val genreFilter = f.genreClauses()
        val formatFilter = f.formatClauses()

        val body =
            Q.obj {
                put("from", JsonPrimitive((query.page - 1) * query.size))
                put("size", JsonPrimitive(query.size))
                put("_source", JsonPrimitive(false))
                put("track_total_hits", JsonPrimitive(true))
                put(
                    "query",
                    Q.bool(
                        must = listOfNotNull(query.q?.let(::textQuery)),
                        should = listOfNotNull(query.q?.let { localeBoost(it, locale) }),
                        filter = base.filter,
                        mustNot = base.mustNot,
                    ),
                )
                // Post-filter: each facet counts with every filter except its own.
                if (genreFilter.isNotEmpty() || formatFilter.isNotEmpty()) {
                    put("post_filter", Q.bool(filter = genreFilter + formatFilter))
                }
                put(
                    "aggs",
                    Q.obj {
                        put("genres", facetAgg(formatFilter, "genres", GENRE_BUCKETS))
                        put("formats", facetAgg(genreFilter, "format", SMALL_BUCKETS))
                        put("decades", facetAgg(genreFilter + formatFilter, "decade", SMALL_BUCKETS))
                    },
                )
                put("sort", sort(query.sort, query.q != null))
            }
        val result = es.search(body)
        val genreNames = meta.genreNames(locale)
        val aggs = result.aggregations
        return SearchResult(
            items = anime.summaries(result.ids, locale, userId),
            total = result.total,
            facets =
                Facets(
                    genres = buckets(aggs, "genres").map { (slug, n) -> GenreFacet(slug, genreNames[slug] ?: slug, n) },
                    formats = buckets(aggs, "formats").map { (v, n) -> ValueFacet(v, n) },
                    decades =
                        buckets(aggs, "decades")
                            .map { (v, n) ->
                                ValueFacet(v, n)
                            }.sortedByDescending { it.value },
                ),
        )
    }

    /** Up to 8 titles matching a prefix, in any script. */
    suspend fun suggest(q: String, locale: AppLocale, userId: UUID?): SummaryList {
        val text = q.trim()
        if (text.isEmpty() ||
            text.length > MAX_SUGGEST
        ) {
            throw Errors.validation("q must be 1 to $MAX_SUGGEST characters")
        }
        val showAdult = userId?.let { users.showAdult(it) } ?: false
        val base = AnimeFilters().baseClauses(showAdult, emptySet(), onlyIds = null)
        val prefix =
            Q.obj {
                put(
                    "multi_match",
                    Q.obj {
                        put("query", JsonPrimitive(text))
                        put("type", JsonPrimitive("bool_prefix"))
                        put(
                            "fields",
                            JsonArray(
                                listOf(
                                    "title_suggest",
                                    "title_suggest._2gram",
                                    "title_suggest._3gram",
                                ).map(::JsonPrimitive),
                            ),
                        )
                    },
                )
            }
        val japanese = match("title_suggest.ja", text)
        val body =
            Q.obj {
                put("size", JsonPrimitive(SUGGEST_SIZE))
                put("_source", JsonPrimitive(false))
                put(
                    "query",
                    Q.bool(
                        must = listOf(Q.bool(should = listOf(prefix, japanese), minimumShouldMatch = 1)),
                        filter = base.filter,
                        mustNot = base.mustNot,
                    ),
                )
                put("sort", JsonArray(listOf(JsonPrimitive("_score"), desc("popularity"))))
            }
        return SummaryList(anime.summaries(es.search(body).ids, locale, userId))
    }

    /**
     * Every title field in every language plus synonyms. Fuzziness only on Latin and Cyrillic
     * fields: fuzzy matching Japanese tokens produces noise.
     */
    private fun textQuery(q: String): JsonObject {
        val fuzzy =
            multiMatch(q, listOf("title.en^3", "title.ru^3", "title.ja_latn^3", "synonyms^2")) {
                put("fuzziness", JsonPrimitive("AUTO"))
            }
        val exact = multiMatch(q, listOf("title.ja^3", "synonyms.ja^2")) { }
        return Q.bool(should = listOf(fuzzy, exact), minimumShouldMatch = 1)
    }

    /** A small boost for matches in the user's own language, so they win ties. */
    private fun localeBoost(q: String, locale: AppLocale) =
        Q.obj {
            put(
                "match",
                Q.obj {
                    put(
                        "title.${locale.key}",
                        Q.obj {
                            put("query", JsonPrimitive(q))
                            put("boost", JsonPrimitive(LOCALE_BOOST))
                        },
                    )
                },
            )
        }

    private fun multiMatch(q: String, fields: List<String>, extra: JsonObjectBuilder.() -> Unit) =
        Q.obj {
            put(
                "multi_match",
                Q.obj {
                    put("query", JsonPrimitive(q))
                    put("fields", JsonArray(fields.map(::JsonPrimitive)))
                    put("operator", JsonPrimitive("and"))
                    extra()
                },
            )
        }

    private fun match(field: String, q: String) = Q.obj { put("match", Q.obj { put(field, JsonPrimitive(q)) }) }

    private fun facetAgg(filters: List<JsonObject>, field: String, size: Int) =
        Q.obj {
            put("filter", Q.bool(filter = filters))
            put(
                "aggs",
                Q.obj {
                    put(
                        "values",
                        Q.obj {
                            put(
                                "terms",
                                Q.obj {
                                    put("field", JsonPrimitive(field))
                                    put("size", JsonPrimitive(size))
                                },
                            )
                        },
                    )
                },
            )
        }

    private fun buckets(aggs: JsonObject?, name: String): List<Pair<String, Long>> =
        aggs
            ?.get(name)
            ?.jsonObject
            ?.get("values")
            ?.jsonObject
            ?.get("buckets")
            ?.jsonArray
            ?.map { it.jsonObject["key"]!!.jsonPrimitive.content to it.jsonObject["doc_count"]!!.jsonPrimitive.long }
            .orEmpty()

    private fun desc(field: String) =
        Q.obj {
            put(
                field,
                Q.obj {
                    put("order", JsonPrimitive("desc"))
                    put("missing", JsonPrimitive("_last"))
                },
            )
        }

    private fun sort(sort: SearchSort, hasQuery: Boolean): JsonArray {
        val tiebreak = Q.obj { put("id", Q.obj { put("order", JsonPrimitive("asc")) }) }
        val keys =
            when (if (sort == SearchSort.RELEVANCE && !hasQuery) SearchSort.POPULARITY else sort) {
                SearchSort.RELEVANCE -> listOf(JsonPrimitive("_score"), desc("popularity"))
                SearchSort.POPULARITY -> listOf(desc("popularity"))
                SearchSort.SCORE -> listOf(desc("score"), desc("popularity"))
                SearchSort.NEWEST -> listOf(desc("season_year"), desc("popularity"))
            }
        return JsonArray(keys + tiebreak)
    }

    companion object {
        const val SUGGEST_SIZE = 8
        const val MAX_SUGGEST = 100
        private const val LOCALE_BOOST = 1.2
        private const val GENRE_BUCKETS = 100
        private const val SMALL_BUCKETS = 20
    }
}
