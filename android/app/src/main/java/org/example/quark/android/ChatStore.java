package org.example.quark.android;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import java.text.DateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.List;

final class ChatStore extends SQLiteOpenHelper {
    ChatStore(Context context) {
        super(context, "quark-history.db", null, 1);
    }

    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE messages (id INTEGER PRIMARY KEY, account TEXT NOT NULL, "
                + "peer TEXT NOT NULL, outgoing INTEGER NOT NULL, body TEXT NOT NULL, "
                + "stanza_id TEXT, time INTEGER NOT NULL)");
        db.execSQL("CREATE INDEX peer_messages ON messages(account, peer, id)");
        db.execSQL("CREATE UNIQUE INDEX message_ids ON messages(account, peer, stanza_id) "
                + "WHERE stanza_id IS NOT NULL");
    }

    @Override public void onUpgrade(SQLiteDatabase db, int previous, int current) { }

    synchronized boolean add(String account, String peer, boolean outgoing, String body, String stanzaId) {
        ContentValues values = new ContentValues();
        values.put("account", account);
        values.put("peer", peer);
        values.put("outgoing", outgoing ? 1 : 0);
        values.put("body", body);
        if (stanzaId != null && !stanzaId.isEmpty()) values.put("stanza_id", stanzaId);
        values.put("time", System.currentTimeMillis());
        return getWritableDatabase().insertWithOnConflict("messages", null, values,
                SQLiteDatabase.CONFLICT_IGNORE) != -1;
    }

    synchronized List<String> recent(String account, String peer, int limit) {
        List<String> lines = new ArrayList<>();
        try (Cursor rows = getReadableDatabase().query("messages",
                new String[]{"outgoing", "body", "time"}, "account=? AND peer=?",
                new String[]{account, peer}, null, null, "id DESC", String.valueOf(limit))) {
            while (rows.moveToNext()) {
                String when = DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT)
                        .format(new Date(rows.getLong(2)));
                lines.add(when + "  " + (rows.getInt(0) != 0 ? "Вы" : peer) + "\n" + rows.getString(1));
            }
        }
        Collections.reverse(lines);
        return lines;
    }

    synchronized List<String> peers(String account) {
        List<String> result = new ArrayList<>();
        try (Cursor rows = getReadableDatabase().rawQuery(
                "SELECT peer FROM messages WHERE account=? GROUP BY peer ORDER BY MAX(id) DESC",
                new String[]{account})) {
            while (rows.moveToNext()) result.add(rows.getString(0));
        }
        return result;
    }
}
