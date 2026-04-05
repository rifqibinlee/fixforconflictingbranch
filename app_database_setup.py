import psycopg2
import os
import sys
import time

DB_CONFIG = {
    'host':     os.getenv('DB_HOST',     'localhost'),
    'port':     int(os.getenv('DB_PORT', '5432')),
    'database': os.getenv('DB_NAME',     'vibe_db'),
    'user':     os.getenv('DB_USER',     'postgres'),
    'password': os.getenv('DB_PASSWORD', '1234'),
}

def run_setup():
    print("=" * 60)
    print("DATABASE SCHEMA SETUP — STATEFUL FEATURES ONLY")
    print("=" * 60)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    cursor = conn.cursor()

    try:
        # --- 1. IAM / USER TABLES ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                full_name     TEXT,
                role          TEXT NOT NULL CHECK (role IN ('Admin','Planner','Staff')),
                is_active     BOOLEAN DEFAULT true,
                created_at    TIMESTAMP DEFAULT NOW(),
                last_login    TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS login_history (
                id         SERIAL PRIMARY KEY,
                user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
                username   TEXT NOT NULL,
                login_time TIMESTAMP DEFAULT NOW(),
                ip_address TEXT,
                user_agent TEXT,
                status     TEXT CHECK (status IN ('success','failed'))
            );
        """)
        print("  [OK] IAM Tables created.")

        # --- 2. MAP ANNOTATIONS ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS map_annotations (
                id                   SERIAL PRIMARY KEY,
                title                TEXT DEFAULT 'Untitled' NOT NULL,
                description          TEXT,
                shape_type           TEXT NOT NULL CHECK (shape_type IN ('point','polyline','polygon','buffer','rectangle','circle')),
                geojson              TEXT NOT NULL,
                representative_lat   DOUBLE PRECISION,
                representative_lng   DOUBLE PRECISION,
                center_lat           DOUBLE PRECISION,
                center_lng           DOUBLE PRECISION,
                radius_meters        DOUBLE PRECISION,
                color                TEXT DEFAULT '#2563eb',
                fill_color           TEXT DEFAULT '#2563eb',
                fill_opacity         DOUBLE PRECISION DEFAULT 0.2,
                stroke_weight        INTEGER DEFAULT 2,
                created_by           INTEGER REFERENCES users(id) ON DELETE CASCADE,
                created_by_username  TEXT NOT NULL,
                assigned_to          INTEGER REFERENCES users(id) ON DELETE SET NULL,
                assigned_to_username TEXT,
                status               TEXT DEFAULT 'open' CHECK (status IN ('open','in_progress','resolved','closed')),
                priority             TEXT DEFAULT 'normal' CHECK (priority IN ('low','normal','high','critical')),
                created_at           TIMESTAMP DEFAULT NOW(),
                updated_at           TIMESTAMP DEFAULT NOW(),
                closed_at            TIMESTAMP,
                days_open            INTEGER
            );

            CREATE TABLE IF NOT EXISTS annotation_assignees (
                annotation_id INTEGER NOT NULL REFERENCES map_annotations(id) ON DELETE CASCADE,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                assigned_at   TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (annotation_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS annotation_comments (
                id              SERIAL PRIMARY KEY,
                annotation_id   INTEGER NOT NULL REFERENCES map_annotations(id) ON DELETE CASCADE,
                author_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                author_username TEXT NOT NULL,
                body            TEXT NOT NULL,
                created_at      TIMESTAMP DEFAULT NOW()
            );
        """)
        print("  [OK] Annotation Tables created.")

        # --- 3. MESSAGING TABLES ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id         SERIAL PRIMARY KEY,
                title      TEXT,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                is_group   BOOLEAN DEFAULT false
            );

            CREATE TABLE IF NOT EXISTS conversation_participants (
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at       TIMESTAMP DEFAULT NOW(),
                is_admin        BOOLEAN DEFAULT false,
                PRIMARY KEY (conversation_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id              SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
                sender_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
                content         TEXT NOT NULL,
                sent_at         TIMESTAMP DEFAULT NOW(),
                is_read         BOOLEAN DEFAULT false,
                annotation_id   INTEGER REFERENCES map_annotations(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS message_reads (
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                read_at    TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (message_id, user_id)
            );
        """)
        print("  [OK] Messaging Tables created.")

        # --- 4. REVIEWS ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id              SERIAL PRIMARY KEY,
                user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                username        TEXT NOT NULL,
                category        TEXT NOT NULL DEFAULT 'General',
                rating          INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                title           TEXT,
                body            TEXT NOT NULL,
                is_anonymous    BOOLEAN NOT NULL DEFAULT false,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
        """)
        print("  [OK] Reviews Table created.")

        # --- 5. CAPEX PRICING (With Default Seed Data) ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS capex_pricing (
                id          SERIAL PRIMARY KEY,
                category    TEXT NOT NULL CHECK (category IN ('EQ', 'ES')),
                action_name TEXT NOT NULL,
                price_myr   NUMERIC(12, 2) NOT NULL CHECK (price_myr >= 0),
                price_min   NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (price_min >= 0),
                price_max   NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (price_max >= price_min),
                updated_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
                updated_at  TIMESTAMP DEFAULT NOW(),
                created_at  TIMESTAMP DEFAULT NOW(),
                UNIQUE (category, action_name)
            );
        """)

        # Seed initial prices if empty
        cursor.execute("SELECT COUNT(*) FROM capex_pricing")
        if cursor.fetchone()[0] == 0:
            prices = [
                ('EQ', 'Add Layer', 30000, 25000, 35000),
                ('EQ', 'BW Upg', 25000, 21000, 29000),
                ('EQ', 'Bi-Sect Radio', 35000, 30000, 40000),
                ('EQ', 'Bi-Sect Antenna + Accessory', 15000, 13000, 17000),
                ('EQ', 'Add Sector IBC', 20000, 17000, 23000),
                ('EQ', 'MM', 60000, 51000, 69000),
                ('EQ', 'Accelerate NIC', 65000, 55000, 75000),
                ('EQ', 'Swap all Sector Radio Ericsson to ZTE', 275000, 234000, 316000),
                ('EQ', 'NNS', 300000, 255000, 345000),
                ('EQ', 'Split Omni to Sector', 225000, 191000, 259000),
                ('ES', 'Add Layer', 32000, 27000, 37000),
                ('ES', 'BW Upg', 25000, 21000, 29000),
                ('ES', 'Bi-Sect', 34000, 29000, 39000),
                ('ES', 'Add Sector IBC', 27000, 23000, 31000),
                ('ES', 'MM', 35000, 30000, 40000),
                ('ES', 'Accelerate NIC', 26000, 22000, 30000),
                ('ES', 'Swap all sector radio Ericsson to ZTE', 41000, 35000, 47000),
                ('ES', 'NNS', 40000, 34000, 46000),
                ('ES', 'Split Omni to Sector', 40000, 34000, 46000),
                ('ES', 'Dismantle', 39000, 33000, 45000)
            ]
            for cat, name, p, pmin, pmax in prices:
                cursor.execute("""
                    INSERT INTO capex_pricing (category, action_name, price_myr, price_min, price_max)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                """, (cat, name, p, pmin, pmax))
            print("  [OK] Default CAPEX Pricing seeded.")

        print("\nSUCCESS: All stateful tables generated perfectly.")

    except Exception as e:
        print(f"\nERROR during setup: {e}")
        sys.exit(1)
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    run_setup()
