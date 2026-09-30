package picker.infra.db

import org.jetbrains.exposed.v1.core.Table

/*
 * Schema `catalog`: owned by the ingest worker, read-only here (the api_svc role only has
 * SELECT). Columns follow services/ingest-worker/contract/catalog-schema.sql; a column
 * this service reads is a contract, so add it in the ingest worker first.
 */

object AnimeTable : Table("catalog.anime") {
    val id = long("id")
    val anilistId = integer("anilist_id")
    val malId = integer("mal_id").nullable()
    val format = text("format").nullable()
    val status = text("status").nullable()
    val season = text("season").nullable()
    val seasonYear = integer("season_year").nullable()
    val episodes = integer("episodes").nullable()
    val durationMin = integer("duration_min").nullable()
    val score = decimal("score", 3, 1).nullable()
    val popularity = integer("popularity").nullable()
    val coverUrl = text("cover_url").nullable()
    val isAdult = bool("is_adult")
    override val primaryKey = PrimaryKey(id)
}

object AnimeLocalizationTable : Table("catalog.anime_localization") {
    val animeId = long("anime_id")
    val locale = text("locale")
    val title = text("title").nullable()
    val synopsis = text("synopsis").nullable()
    val titleMachine = bool("title_machine")
    val synopsisMachine = bool("synopsis_machine")
}

object GenreTable : Table("catalog.genre") {
    val id = long("id")
    val slug = varchar("slug", 50)
}

object GenreLocalizationTable : Table("catalog.genre_localization") {
    val genreId = long("genre_id")
    val locale = text("locale")
    val name = text("name")
}

object TagTable : Table("catalog.tag") {
    val id = long("id")
    val slug = varchar("slug", 100)
    val category = text("category").nullable()
    val isSpoiler = bool("is_spoiler")
}

object TagLocalizationTable : Table("catalog.tag_localization") {
    val tagId = long("tag_id")
    val locale = text("locale")
    val name = text("name")
}

object AnimeGenreTable : Table("catalog.anime_genre") {
    val animeId = long("anime_id")
    val genreId = long("genre_id")
}

object AnimeTagTable : Table("catalog.anime_tag") {
    val animeId = long("anime_id")
    val tagId = long("tag_id")
    val rank = short("rank")
    val isSpoiler = bool("is_spoiler")
}

object StudioTable : Table("catalog.studio") {
    val id = long("id")
    val name = text("name")
}

object AnimeStudioTable : Table("catalog.anime_studio") {
    val animeId = long("anime_id")
    val studioId = long("studio_id")
    val isMain = bool("is_main")
}

object AnimeRelationTable : Table("catalog.anime_relation") {
    val animeId = long("anime_id")
    val relatedId = long("related_id")
    val kind = text("kind")
}
