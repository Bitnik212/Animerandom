package picker.auth

import io.ktor.server.application.ApplicationCall
import io.ktor.server.auth.principal
import picker.errors.Errors
import java.util.UUID

/** The authenticated user, taken from the validated access token only. */
data class UserPrincipal(val id: UUID, val email: String?, val roles: Set<String>, val tokenId: String?) {
    fun isAdmin() = ADMIN_ROLE in roles

    companion object {
        const val ADMIN_ROLE = "admin"
    }
}

fun ApplicationCall.userOrNull(): UserPrincipal? = principal<UserPrincipal>()

fun ApplicationCall.user(): UserPrincipal = userOrNull() ?: throw Errors.unauthorized()

fun ApplicationCall.requireAdmin(): UserPrincipal =
    user().also {
        if (!it.isAdmin()) throw Errors.forbidden("The admin role is required")
    }
