package picker.infra.redis

import io.lettuce.core.ExperimentalLettuceCoroutinesApi
import io.lettuce.core.ScanArgs
import io.lettuce.core.ScanCursor
import java.util.UUID

/** Drops every `user:{id}:*` key (account deletion). */
@OptIn(ExperimentalLettuceCoroutinesApi::class)
suspend fun Redis.deleteUserKeys(userId: UUID) {
    var cursor: ScanCursor = ScanCursor.INITIAL
    do {
        val page = commands.scan(cursor, ScanArgs.Builder.matches(Keys.userPattern(userId)).limit(500)) ?: break
        page.keys.forEach { commands.unlink(it) }
        cursor = page
    } while (!page.isFinished)
}
