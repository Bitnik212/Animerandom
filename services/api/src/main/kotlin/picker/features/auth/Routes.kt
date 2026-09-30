package picker.features.auth

import io.ktor.http.HttpStatusCode
import io.ktor.server.plugins.origin
import io.ktor.server.request.receive
import io.ktor.server.response.respond
import io.ktor.server.routing.Route
import io.ktor.server.routing.delete
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.routing.put
import io.ktor.server.routing.route
import picker.auth.requireAdmin
import picker.auth.user
import picker.errors.Errors
import java.util.UUID

/** The public `/v1/auth` endpoints. Request bodies here are never logged. */
fun Route.authRoutes(service: AuthService) {
    route("/auth") {
        post("/signup") {
            val result = service.signUp(call.receive(), call.request.origin.remoteHost)
            if (result.verificationRequired) {
                call.respond(HttpStatusCode.Created, VerificationRequired())
            } else {
                call.respond(HttpStatusCode.Created, result.tokens!!)
            }
        }
        post("/signin") { call.respond(service.signIn(call.receive(), call.request.origin.remoteHost)) }
        post("/refresh") { call.respond(service.refresh(call.receive())) }
        post("/signout") {
            service.signOut(call.receive())
            call.respond(HttpStatusCode.NoContent)
        }
        post("/password/forgot") {
            service.forgotPassword(call.receive(), call.request.origin.remoteHost)
            call.respond(HttpStatusCode.Accepted)
        }
    }
}

/** Account endpoints under /v1/users/me that go through Keycloak. Requires a token. */
fun Route.accountRoutes(accounts: AccountService) {
    route("/users/me") {
        put("/password") {
            accounts.changePassword(call.user(), call.receive())
            call.respond(HttpStatusCode.NoContent)
        }
        post("/logout-all") {
            accounts.logoutEverywhere(call.user().id)
            call.respond(HttpStatusCode.NoContent)
        }
        delete {
            accounts.deleteAccount(call.user().id)
            call.respond(HttpStatusCode.NoContent)
        }
    }
}

/** The `/v1/admin` endpoints: require the admin role. */
fun Route.adminRoutes(accounts: AccountService) {
    route("/admin/users/{id}") {
        get {
            call.requireAdmin()
            call.respond(accounts.adminView(userId(call.parameters["id"])))
        }
        put("/admin") {
            call.requireAdmin()
            accounts.setAdmin(userId(call.parameters["id"]), true)
            call.respond(HttpStatusCode.NoContent)
        }
        delete("/admin") {
            call.requireAdmin()
            accounts.setAdmin(userId(call.parameters["id"]), false)
            call.respond(HttpStatusCode.NoContent)
        }
    }
}

private fun userId(value: String?): UUID =
    runCatching { UUID.fromString(value) }.getOrNull() ?: throw Errors.userNotFound("Not a user id: $value")
