package picker.infra.db

import org.jetbrains.exposed.v1.core.Table
import org.jetbrains.exposed.v1.core.java.javaUUID
import org.jetbrains.exposed.v1.javatime.timestampWithTimeZone

/** Schema `app`: owned and written by this service. */
object AppUserTable : Table("app.app_user") {
    val id = javaUUID("id")
    val displayName = text("display_name").nullable()
    val locale = text("locale")
    val showAdult = bool("show_adult")
    val onboardingCompletedAt = timestampWithTimeZone("onboarding_completed_at").nullable()
    val lastSeenAt = timestampWithTimeZone("last_seen_at").nullable()
    val createdAt = timestampWithTimeZone("created_at")
    val likedGenres = array<String>("liked_genres")
    val dislikedGenres = array<String>("disliked_genres")
    override val primaryKey = PrimaryKey(id)
}

object UserAnimeTable : Table("app.user_anime") {
    val userId = javaUUID("user_id")
    val animeId = long("anime_id")
    val status = text("status")
    val score = short("score").nullable()
    val createdAt = timestampWithTimeZone("created_at")
    val updatedAt = timestampWithTimeZone("updated_at")
    override val primaryKey = PrimaryKey(userId, animeId)
}

object UserFeedbackTable : Table("app.user_feedback") {
    val id = long("id").autoIncrement()
    val userId = javaUUID("user_id")
    val animeId = long("anime_id")
    val kind = text("kind")
    val createdAt = timestampWithTimeZone("created_at")
    override val primaryKey = PrimaryKey(id)
}
