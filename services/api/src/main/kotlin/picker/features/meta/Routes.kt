package picker.features.meta

import io.ktor.server.response.respond
import io.ktor.server.routing.Route
import io.ktor.server.routing.get
import io.ktor.server.routing.route
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.i18n.locale

fun Route.metaRoutes(service: MetaService, users: UserRepository, defaultLocale: AppLocale) {
    route("/meta") {
        get("/genres") {
            call.respond(service.genres(call.locale(users, defaultLocale)))
        }
        get("/tags") {
            call.respond(service.tags(call.locale(users, defaultLocale), call.request.queryParameters["q"]))
        }
    }
}
