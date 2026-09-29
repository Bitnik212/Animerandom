package picker.features.random

import io.lettuce.core.ExperimentalLettuceCoroutinesApi
import kotlinx.coroutines.flow.toList
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonPrimitive
import picker.errors.Errors
import picker.features.anime.AnimeService
import picker.features.anime.AnimeSummary
import picker.features.search.AnimeFilters
import picker.features.users.Exclusions
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.infra.es.Es
import picker.infra.es.Q
import picker.infra.redis.Keys
import picker.infra.redis.Redis
import java.util.UUID

/** `GET /anime/random` parameters (README "Random"). */
data class RandomQuery(
    val filters: AnimeFilters,
    val hideWatched: Boolean?,
    val count: Int,
    val watchlist: Boolean,
) {
    companion object {
        const val MAX_COUNT = 10

        fun parse(all: (String) -> List<String>): RandomQuery {
            fun one(name: String) = all(name).firstOrNull()
            val source = one("source") ?: "catalog"
            if (source !in setOf("catalog", "watchlist")) throw Errors.validation("source must be catalog or watchlist")
            return RandomQuery(
                filters =
                    AnimeFilters(
                        genres = AnimeFilters.slugs("genre", all("genre")),
                        excludeGenres = AnimeFilters.slugs("excludeGenre", all("excludeGenre")),
                        tags = AnimeFilters.slugs("tag", all("tag")),
                        formats = AnimeFilters.enums("format", listOfNotNull(one("format"))),
                        decade = AnimeFilters.decade(one("decade")),
                        length = AnimeFilters.length(one("length")),
                        yearFrom = AnimeFilters.int("yearFrom", one("yearFrom"), AnimeFilters.YEARS),
                        yearTo = AnimeFilters.int("yearTo", one("yearTo"), AnimeFilters.YEARS),
                        minScore = AnimeFilters.score(one("minScore")),
                    ),
                hideWatched = AnimeFilters.bool("hideWatched", one("hideWatched")),
                count = AnimeFilters.int("count", one("count"), 1..MAX_COUNT) ?: 1,
                watchlist = source == "watchlist",
            )
        }
    }
}

@Serializable
data class RandomResult(val items: List<AnimeSummary>, val poolSize: Long)

/**
 * Random picks (README "Random selection"). Pooled filters are answered from the `pool:*` sets
 * the ingest worker maintains; anything the pools can't express, or a user who opted into adult
 * titles (never pooled), goes to Elasticsearch with `random_score`.
 */
@OptIn(ExperimentalLettuceCoroutinesApi::class)
class RandomService(
    private val redis: Redis,
    private val es: Es,
    private val anime: AnimeService,
    private val users: UserRepository,
    private val exclusions: Exclusions,
) {
    suspend fun pick(query: RandomQuery, locale: AppLocale, userId: UUID?): RandomResult {
        if (query.watchlist && userId == null) throw Errors.unauthorized("source=watchlist needs a signed-in user")
        val hideWatched = userId != null && (query.hideWatched ?: true)
        val showAdult = userId?.let { users.showAdult(it) } ?: false
        val planned = if (query.watchlist) users.plannedIds(userId!!) else null
        if (planned != null && planned.isEmpty()) return RandomResult(emptyList(), 0)

        val (ids, size) =
            if (!showAdult && pooled(query.filters)) {
                fromPools(query, planned, if (hideWatched) userId else null)
            } else {
                val excluded = if (hideWatched) exclusions.ids(userId!!) else emptySet()
                fromSearch(query, showAdult, excluded, planned)
            }
        return RandomResult(anime.summaries(ids, locale, userId), size)
    }

    /** True when every filter maps onto a pool. `minScore` below 6 has no bucket. */
    private fun pooled(f: AnimeFilters) =
        f.tags.isEmpty() && f.yearFrom == null && f.yearTo == null && (f.minScore == null || f.minScore >= MIN_BUCKET)

    private suspend fun fromPools(query: RandomQuery, planned: Set<Long>?, excludeFor: UUID?): Pair<List<Long>, Long> {
        val f = query.filters
        val scratch = mutableListOf<String>()
        try {
            val include =
                buildList {
                    f.genres.forEach { add(Keys.poolGenre(it)) }
                    f.formats.forEach { add(Keys.poolFormat(it)) }
                    f.minScore?.let { add(Keys.poolScore(it.toInt().coerceAtMost(MAX_BUCKET))) }
                    f.decade?.let { add(Keys.poolDecade(it)) }
                    f.length?.let { add(Keys.poolLength(it)) }
                    planned?.let { ids ->
                        val key = Keys.randomScratch().also(scratch::add)
                        redis.commands.sadd(key, *ids.map(Long::toString).toTypedArray())
                        redis.commands.expire(key, SCRATCH_TTL_SECONDS)
                        add(key)
                    }
                }.ifEmpty { listOf(Keys.POOL_ALL) }
            val exclude =
                f.excludeGenres.map(Keys::poolGenre) + listOfNotNull(excludeFor?.let { exclusions.ensure(it) })

            val result = Keys.randomScratch().also(scratch::add)
            redis.commands.sinterstore(result, *include.toTypedArray())
            redis.commands.expire(result, SCRATCH_TTL_SECONDS)
            if (exclude.isNotEmpty()) redis.commands.sdiffstore(result, result, *exclude.toTypedArray())
            val size = redis.commands.scard(result) ?: 0L
            val ids =
                redis.commands
                    .srandmember(result, query.count.toLong())
                    .toList()
                    .map(String::toLong)
            return ids to size
        } finally {
            if (scratch.isNotEmpty()) redis.commands.del(*scratch.toTypedArray())
        }
    }

    private suspend fun fromSearch(
        query: RandomQuery,
        showAdult: Boolean,
        excluded: Set<Long>,
        planned: Set<Long>?,
    ): Pair<List<Long>, Long> {
        val f = query.filters
        val clauses = f.baseClauses(showAdult, excluded, planned)
        val body =
            Q.obj {
                put("size", JsonPrimitive(query.count))
                put("_source", JsonPrimitive(false))
                put("track_total_hits", JsonPrimitive(true))
                put(
                    "query",
                    Q.obj {
                        put(
                            "function_score",
                            Q.obj {
                                put(
                                    "query",
                                    Q.bool(
                                        filter = f.genreClauses() + f.formatClauses() + clauses.filter,
                                        mustNot = clauses.mustNot,
                                    ),
                                )
                                put("random_score", Q.obj { })
                                put("boost_mode", JsonPrimitive("replace"))
                            },
                        )
                    },
                )
            }
        val result = es.search(body)
        return result.ids to result.total
    }

    companion object {
        const val SCRATCH_TTL_SECONDS = 10L
        private const val MIN_BUCKET = 6
        private const val MAX_BUCKET = 9
    }
}
