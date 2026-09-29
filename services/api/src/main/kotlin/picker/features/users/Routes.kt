package picker.features.users

import io.ktor.http.HttpStatusCode
import io.ktor.server.application.ApplicationCall
import io.ktor.server.request.receive
import io.ktor.server.response.respond
import io.ktor.server.routing.Route
import io.ktor.server.routing.delete
import io.ktor.server.routing.get
import io.ktor.server.routing.patch
import io.ktor.server.routing.post
import io.ktor.server.routing.put
import io.ktor.server.routing.route
import kotlinx.serialization.json.JsonObject
import picker.auth.user
import picker.errors.Errors
import picker.features.anime.intIn
import picker.i18n.AppLocale
import picker.i18n.locale

/** `/users/me` profile, onboarding, list and feedback. Password, logout-all and deletion live in features/auth. */
fun Route.userRoutes(service: UsersService, users: UserRepository, defaultLocale: AppLocale) {
    route("/users/me") {
        get { call.respond(service.profile(call.user())) }
        patch { call.respond(service.update(call.user(), call.receive<JsonObject>())) }
        post("/onboarding") {
            service.onboard(call.user().id, call.receive())
            call.respond(HttpStatusCode.NoContent)
        }
        route("/anime") {
            get {
                val params = call.request.queryParameters
                val sort =
                    params["sort"]?.let { value ->
                        ListSort.entries.firstOrNull { it.name.equals(value, ignoreCase = true) }
                            ?: throw Errors.validation("sort must be added, score or length")
                    } ?: ListSort.ADDED
                val page = params["page"].intIn("page", 1..MAX_PAGE, 1)
                val size = params["size"].intIn("size", 1..MAX_SIZE, DEFAULT_SIZE)
                call.respond(
                    service.list(call.user().id, params["status"], sort, page, size, call.locale(users, defaultLocale)),
                )
            }
            put("/{animeId}") {
                val id = call.animeId()
                call.respond(service.setEntry(call.user().id, id, call.receive(), call.locale(users, defaultLocale)))
            }
            delete("/{animeId}") {
                service.removeEntry(call.user().id, call.animeId())
                call.respond(HttpStatusCode.NoContent)
            }
        }
        post("/feedback") {
            service.feedback(call.user().id, call.receive())
            call.respond(HttpStatusCode.NoContent)
        }
    }
}

private const val MAX_PAGE = 10_000
private const val MAX_SIZE = 50
private const val DEFAULT_SIZE = 20

private fun ApplicationCall.animeId(): Long =
    parameters["animeId"]?.toLongOrNull()?.takeIf { it > 0 }
        ?: throw Errors.validation("animeId must be a positive integer")
