package picker.auth

import io.ktor.server.application.createRouteScopedPlugin
import io.ktor.server.auth.AuthenticationChecked
import java.util.Collections
import java.util.UUID

/** Creates the `app_user` row for users that have none yet (e.g. made in the Keycloak console). */
fun interface AppUserProvisioner {
    suspend fun ensure(principal: UserPrincipal)
}

class EnsureAppUserConfig {
    lateinit var provisioner: AppUserProvisioner
}

/**
 * Runs after authentication on every route it's installed on. Remembers the last few
 * thousand ids it provisioned, so steady traffic doesn't cost an insert per request.
 */
val EnsureAppUser =
    createRouteScopedPlugin("EnsureAppUser", ::EnsureAppUserConfig) {
        val provisioner = pluginConfig.provisioner
        val seen: MutableSet<UUID> =
            Collections.newSetFromMap(
                Collections.synchronizedMap(
                    object : LinkedHashMap<UUID, Boolean>(1024, 0.75f, true) {
                        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<UUID, Boolean>) = size > 5000
                    },
                ),
            )
        on(AuthenticationChecked) { call ->
            val principal = call.userOrNull() ?: return@on
            if (principal.id !in seen) {
                provisioner.ensure(principal)
                seen.add(principal.id)
            }
        }
    }
