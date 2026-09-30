package picker.features.anime

import io.ktor.server.response.respond
import io.ktor.server.routing.Route
import io.ktor.server.routing.get
import io.ktor.server.routing.route
import picker.auth.userOrNull
import picker.errors.Errors
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.i18n.locale

/** `/anime/{id}` and `/anime/{id}/similar`. `/anime/random` lives in features/random. */
fun Route.animeRoutes(service: AnimeService, users: UserRepository, defaultLocale: AppLocale) {
    route("/anime/{id}") {
        get {
            val id = call.animeId()
            call.respond(service.details(id, call.locale(users, defaultLocale), call.userOrNull()?.id))
        }
        get("/similar") {
            val id = call.animeId()
            val limit = call.request.queryParameters["limit"].intIn("limit", 1..MAX_SIMILAR, DEFAULT_SIMILAR)
            call.respond(service.similar(id, limit, call.locale(users, defaultLocale), call.userOrNull()?.id))
        }
    }
}

private const val MAX_SIMILAR = 50
private const val DEFAULT_SIMILAR = 20

private fun io.ktor.server.application.ApplicationCall.animeId(): Long =
    parameters["id"]?.toLongOrNull()?.takeIf { it > 0 } ?: throw Errors.validation("id must be a positive integer")

/** Optional integer query value within [range]; [default] when absent. */
fun String?.intIn(name: String, range: IntRange, default: Int): Int {
    if (this == null) return default
    val value = toIntOrNull() ?: throw Errors.validation("$name must be an integer")
    if (value !in range) throw Errors.validation("$name must be between ${range.first} and ${range.last}")
    return value
}
