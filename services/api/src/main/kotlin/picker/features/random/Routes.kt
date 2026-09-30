package picker.features.random

import io.ktor.server.response.respond
import io.ktor.server.routing.Route
import io.ktor.server.routing.get
import picker.auth.userOrNull
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.i18n.locale

fun Route.randomRoutes(service: RandomService, users: UserRepository, defaultLocale: AppLocale) {
    get("/anime/random") {
        val query =
            RandomQuery.parse {
                call.request.queryParameters
                    .getAll(it)
                    .orEmpty()
            }
        call.respond(service.pick(query, call.locale(users, defaultLocale), call.userOrNull()?.id))
    }
}
