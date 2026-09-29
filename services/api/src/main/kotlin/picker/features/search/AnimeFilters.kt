package picker.features.search

import kotlinx.serialization.json.JsonObject
import picker.errors.Errors
import picker.infra.es.Q

/**
 * Catalog filters shared by random picks and search. Parsed from query parameters by the
 * endpoint that owns them; values are validated here so every path sees clean input.
 */
data class AnimeFilters(
    val genres: List<String> = emptyList(),
    val excludeGenres: List<String> = emptyList(),
    val tags: List<String> = emptyList(),
    val excludeTags: List<String> = emptyList(),
    val formats: List<String> = emptyList(),
    val statuses: List<String> = emptyList(),
    val decade: String? = null,
    val length: String? = null,
    val yearFrom: Int? = null,
    val yearTo: Int? = null,
    val minScore: Double? = null,
    val episodesMin: Int? = null,
    val episodesMax: Int? = null,
) {
    /** Genre filter clauses (all must match); kept apart so search can post-filter them for facets. */
    fun genreClauses(): List<JsonObject> = genres.map { Q.term("genres", it) }

    fun formatClauses(): List<JsonObject> = if (formats.isEmpty()) emptyList() else listOf(Q.terms("format", formats))

    /** Every other filter, plus the rules that always apply (no `REMOVED`, adult only on opt-in). */
    fun baseClauses(showAdult: Boolean, excludedIds: Collection<Long>, onlyIds: Collection<Long>?): Clauses =
        Clauses(filterClauses(showAdult, onlyIds), mustNotClauses(excludedIds))

    private fun filterClauses(showAdult: Boolean, onlyIds: Collection<Long>?): List<JsonObject> =
        buildList {
            tags.forEach { add(Q.term("tags", it)) }
            if (statuses.isNotEmpty()) add(Q.terms("status", statuses))
            decade?.let { add(Q.term("decade", it)) }
            length?.let { add(Q.term("length", it)) }
            if (yearFrom != null || yearTo != null) add(Q.range("season_year", yearFrom, yearTo))
            minScore?.let { add(Q.range("score", gte = it)) }
            if (episodesMin != null || episodesMax != null) add(Q.range("episodes", episodesMin, episodesMax))
            onlyIds?.let { add(Q.terms("id", it)) }
            if (!showAdult) add(Q.term("is_adult", false))
        }

    private fun mustNotClauses(excludedIds: Collection<Long>): List<JsonObject> =
        buildList {
            add(Q.term("status", REMOVED))
            if (excludeGenres.isNotEmpty()) add(Q.terms("genres", excludeGenres))
            if (excludeTags.isNotEmpty()) add(Q.terms("tags", excludeTags))
            if (excludedIds.isNotEmpty()) add(Q.terms("id", excludedIds))
        }

    data class Clauses(val filter: List<JsonObject>, val mustNot: List<JsonObject>)

    companion object {
        const val REMOVED = "REMOVED"
        const val MAX_VALUES = 20
        val LENGTHS = setOf("short", "medium", "long")
        private val SLUG = Regex("^[a-z0-9][a-z0-9-]{0,99}$")
        private val ENUM = Regex("^[A-Z][A-Z_]{1,19}$")
        private val DECADE = Regex("^(19|20)\\d0s$")

        fun slugs(name: String, values: List<String>): List<String> = values.check(name, SLUG).distinct()

        fun enums(name: String, values: List<String>): List<String> =
            values
                .map { it.uppercase() }
                .check(
                    name,
                    ENUM,
                ).distinct()

        fun decade(value: String?): String? =
            value?.also {
                if (!DECADE.matches(it)) throw Errors.validation("decade must look like 2010s")
            }

        fun length(value: String?): String? =
            value?.also {
                if (it !in
                    LENGTHS
                ) {
                    throw Errors.validation("length must be one of ${LENGTHS.joinToString()}")
                }
            }

        fun int(name: String, value: String?, range: IntRange): Int? =
            value?.let {
                val v = it.toIntOrNull() ?: throw Errors.validation("$name must be an integer")
                if (v !in range) throw Errors.validation("$name must be between ${range.first} and ${range.last}")
                v
            }

        fun score(value: String?): Double? =
            value?.let {
                val v = it.toDoubleOrNull() ?: throw Errors.validation("minScore must be a number")
                if (v !in 0.0..MAX_SCORE) throw Errors.validation("minScore must be between 0 and 10")
                v
            }

        fun bool(name: String, value: String?): Boolean? =
            when (value?.lowercase()) {
                null -> null
                "true", "1" -> true
                "false", "0" -> false
                else -> throw Errors.validation("$name must be true or false")
            }

        private fun List<String>.check(name: String, pattern: Regex): List<String> {
            if (size > MAX_VALUES) throw Errors.validation("At most $MAX_VALUES values for $name")
            forEach { if (!pattern.matches(it)) throw Errors.validation("Invalid $name '$it'") }
            return this
        }

        private const val MAX_SCORE = 10.0
        val YEARS = 1900..2100
    }
}
