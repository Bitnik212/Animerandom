package picker.features.anime

import org.jetbrains.exposed.v1.core.JoinType
import org.jetbrains.exposed.v1.core.SortOrder
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.core.inList
import org.jetbrains.exposed.v1.jdbc.selectAll
import picker.i18n.AppLocale
import picker.infra.db.AnimeGenreTable
import picker.infra.db.AnimeLocalizationTable
import picker.infra.db.AnimeRelationTable
import picker.infra.db.AnimeStudioTable
import picker.infra.db.AnimeTable
import picker.infra.db.AnimeTagTable
import picker.infra.db.Db
import picker.infra.db.GenreTable
import picker.infra.db.StudioTable
import picker.infra.db.TagTable
import picker.infra.db.tx

data class LocalizedText(
    val title: String?,
    val synopsis: String?,
    val titleMachine: Boolean,
    val synopsisMachine: Boolean,
)

/** One anime as stored, before localization. */
data class AnimeRow(
    val id: Long,
    val format: String?,
    val status: String?,
    val seasonYear: Int?,
    val episodes: Int?,
    val score: Double?,
    val coverUrl: String?,
    val isAdult: Boolean,
    val texts: Map<AppLocale, LocalizedText>,
    val genres: List<String>,
)

data class AnimeTagRow(val slug: String, val rank: Int, val spoiler: Boolean)

data class AnimeExtras(
    val tags: List<AnimeTagRow>,
    val studios: List<Pair<String, Boolean>>,
    val relations: List<Pair<String, Long>>,
)

/** Reads `catalog.*` (schema-qualified, read-only). */
class AnimeRepository(private val db: Db) {
    /** Rows for [ids]; missing IDs are simply absent. Three queries whatever the count. */
    suspend fun rows(ids: Collection<Long>): Map<Long, AnimeRow> {
        if (ids.isEmpty()) return emptyMap()
        return db.tx {
            val texts =
                AnimeLocalizationTable
                    .selectAll()
                    .where { AnimeLocalizationTable.animeId inList ids }
                    .groupBy({ it[AnimeLocalizationTable.animeId] }) { row ->
                        AppLocale.fromKey(row[AnimeLocalizationTable.locale]) to
                            LocalizedText(
                                row[AnimeLocalizationTable.title],
                                row[AnimeLocalizationTable.synopsis],
                                row[AnimeLocalizationTable.titleMachine],
                                row[AnimeLocalizationTable.synopsisMachine],
                            )
                    }
            val genres =
                AnimeGenreTable
                    .join(GenreTable, JoinType.INNER, AnimeGenreTable.genreId, GenreTable.id)
                    .selectAll()
                    .where { AnimeGenreTable.animeId inList ids }
                    .orderBy(GenreTable.slug to SortOrder.ASC)
                    .groupBy({ it[AnimeGenreTable.animeId] }) { it[GenreTable.slug] }
            AnimeTable
                .selectAll()
                .where { AnimeTable.id inList ids }
                .associate { row ->
                    val id = row[AnimeTable.id]
                    id to
                        AnimeRow(
                            id = id,
                            format = row[AnimeTable.format],
                            status = row[AnimeTable.status],
                            seasonYear = row[AnimeTable.seasonYear],
                            episodes = row[AnimeTable.episodes],
                            score = row[AnimeTable.score]?.toDouble(),
                            coverUrl = row[AnimeTable.coverUrl],
                            isAdult = row[AnimeTable.isAdult],
                            texts = texts[id].orEmpty().mapNotNull { (l, t) -> l?.let { it to t } }.toMap(),
                            genres = genres[id].orEmpty(),
                        )
                }
        }
    }

    /** Tags by rank, studios (main first), and relations of one anime. */
    suspend fun extras(id: Long): AnimeExtras =
        db.tx {
            val tags =
                AnimeTagTable
                    .join(TagTable, JoinType.INNER, AnimeTagTable.tagId, TagTable.id)
                    .selectAll()
                    .where { AnimeTagTable.animeId eq id }
                    .orderBy(AnimeTagTable.rank to SortOrder.DESC)
                    .map {
                        AnimeTagRow(
                            it[TagTable.slug],
                            it[AnimeTagTable.rank].toInt(),
                            it[AnimeTagTable.isSpoiler] || it[TagTable.isSpoiler],
                        )
                    }
            val studios =
                AnimeStudioTable
                    .join(StudioTable, JoinType.INNER, AnimeStudioTable.studioId, StudioTable.id)
                    .selectAll()
                    .where { AnimeStudioTable.animeId eq id }
                    .orderBy(AnimeStudioTable.isMain to SortOrder.DESC, StudioTable.name to SortOrder.ASC)
                    .map { it[StudioTable.name] to it[AnimeStudioTable.isMain] }
            val relations =
                AnimeRelationTable
                    .selectAll()
                    .where { AnimeRelationTable.animeId eq id }
                    .orderBy(AnimeRelationTable.kind to SortOrder.ASC, AnimeRelationTable.relatedId to SortOrder.ASC)
                    .map { it[AnimeRelationTable.kind] to it[AnimeRelationTable.relatedId] }
            AnimeExtras(tags, studios, relations)
        }
}
