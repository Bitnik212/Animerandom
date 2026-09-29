package picker.features.search

import io.ktor.server.response.respond
import io.ktor.server.routing.Route
import io.ktor.server.routing.get
import io.ktor.server.routing.route
import picker.auth.userOrNull
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.i18n.locale

fun Route.searchRoutes(service: SearchService, users: UserRepository, defaultLocale: AppLocale) {
    route("/search") {
        get {
            val query =
                SearchQuery.parse {
                    call.request.queryParameters
                        .getAll(it)
                        .orEmpty()
                }
            call.respond(service.search(query, call.locale(users, defaultLocale), call.userOrNull()?.id))
        }
        get("/suggest") {
            val q = call.request.queryParameters["q"].orEmpty()
            call.respond(service.suggest(q, call.locale(users, defaultLocale), call.userOrNull()?.id))
        }
    }
}
