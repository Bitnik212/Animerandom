package picker.features.recommendations

import io.ktor.server.response.respond
import io.ktor.server.routing.Route
import io.ktor.server.routing.get
import picker.auth.user
import picker.features.anime.intIn
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.i18n.locale

fun Route.recommendationRoutes(service: RecommendationService, users: UserRepository, defaultLocale: AppLocale) {
    get("/users/me/recommendations") {
        val limit = call.request.queryParameters["limit"].intIn("limit", 1..MAX_LIMIT, DEFAULT_LIMIT)
        call.respond(service.feed(call.user().id, limit, call.locale(users, defaultLocale)))
    }
}

private const val MAX_LIMIT = 50
private const val DEFAULT_LIMIT = 20
