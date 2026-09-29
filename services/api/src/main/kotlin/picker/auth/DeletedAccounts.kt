package picker.auth

import io.lettuce.core.ExperimentalLettuceCoroutinesApi
import io.lettuce.core.SetArgs
import picker.infra.redis.Keys
import picker.infra.redis.Redis
import java.util.UUID

/**
 * Tombstones for deleted accounts (`deleted:user:{id}`). Access tokens are validated locally and
 * stay valid for minutes after the Keycloak user is gone; without this, such a token would
 * re-create the `app_user` row the deletion just removed. Kept well past the token lifetime.
 */
@OptIn(ExperimentalLettuceCoroutinesApi::class)
class DeletedAccounts(private val redis: Redis) {
    suspend fun mark(userId: UUID) {
        redis.commands.set(Keys.deletedUser(userId), "1", SetArgs.Builder.ex(TTL_SECONDS))
    }

    suspend fun isDeleted(userId: UUID): Boolean = redis.commands.exists(Keys.deletedUser(userId)) == 1L

    companion object {
        const val TTL_SECONDS = 3600L
    }
}
