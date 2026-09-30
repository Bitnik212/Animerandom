package picker.features.meta

import org.jetbrains.exposed.v1.core.JoinType
import org.jetbrains.exposed.v1.core.SortOrder
import org.jetbrains.exposed.v1.jdbc.selectAll
import picker.i18n.AppLocale
import picker.infra.db.Db
import picker.infra.db.GenreLocalizationTable
import picker.infra.db.GenreTable
import picker.infra.db.TagLocalizationTable
import picker.infra.db.TagTable
import picker.infra.db.tx

data class VocabTerm(
    val slug: String,
    val names: Map<AppLocale, String>,
    val category: String? = null,
    val isSpoiler: Boolean = false,
)

class MetaRepository(private val db: Db) {
    suspend fun genres(): List<VocabTerm> =
        db.tx {
            val rows =
                GenreTable
                    .join(GenreLocalizationTable, JoinType.LEFT, GenreTable.id, GenreLocalizationTable.genreId)
                    .selectAll()
                    .orderBy(GenreTable.slug to SortOrder.ASC)
            rows.groupBy { it[GenreTable.slug] }.map { (slug, group) ->
                VocabTerm(
                    slug,
                    group
                        .mapNotNull { row ->
                            row
                                .getOrNull(GenreLocalizationTable.locale)
                                ?.let { AppLocale.fromKey(it) }
                                ?.let { it to row[GenreLocalizationTable.name] }
                        }.toMap(),
                )
            }
        }

    suspend fun tags(): List<VocabTerm> =
        db.tx {
            val rows =
                TagTable
                    .join(TagLocalizationTable, JoinType.LEFT, TagTable.id, TagLocalizationTable.tagId)
                    .selectAll()
                    .orderBy(TagTable.slug to SortOrder.ASC)
            rows.groupBy { it[TagTable.slug] }.map { (slug, group) ->
                val first = group.first()
                VocabTerm(
                    slug,
                    group
                        .mapNotNull { row ->
                            row
                                .getOrNull(TagLocalizationTable.locale)
                                ?.let { AppLocale.fromKey(it) }
                                ?.let { it to row[TagLocalizationTable.name] }
                        }.toMap(),
                    first[TagTable.category],
                    first[TagTable.isSpoiler],
                )
            }
        }
}
