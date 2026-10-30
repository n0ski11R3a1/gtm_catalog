import 'dart:convert';
import 'dart:math';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:sqflite_sqlcipher/sqflite.dart';

class LocalStore {
  LocalStore(this._secureStorage);

  static const _databaseKeyName = 'gtm_mobile_database_key_v1';
  static const _databaseFileName = 'gtm_field_orders.db';

  final FlutterSecureStorage _secureStorage;
  Database? _database;

  Future<Database> get database async {
    if (_database != null) return _database!;
    final directory = await getApplicationDocumentsDirectory();
    final key = await _getOrCreateDatabaseKey();
    _database = await openDatabase(
      p.join(directory.path, _databaseFileName),
      password: key,
      version: 1,
      onCreate: (db, version) async {
        await db.execute('''
          CREATE TABLE catalog_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            snapshot_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL
          )
        ''');
        await db.execute('''
          CREATE TABLE local_identity (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            rep_id INTEGER NOT NULL
          )
        ''');
        await db.execute('''
          CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            upc INTEGER NOT NULL,
            unit TEXT NOT NULL,
            retail REAL NOT NULL,
            wholesale REAL NOT NULL,
            category TEXT NOT NULL,
            status TEXT NOT NULL,
            description TEXT NOT NULL
          )
        ''');
        await db.execute('''
          CREATE TABLE orders (
            client_order_id TEXT PRIMARY KEY,
            outlet_name TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            catalog_snapshot_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued', 'synced', 'review')),
            server_order_id INTEGER,
            price_mismatch INTEGER NOT NULL DEFAULT 0,
            sync_error TEXT
          )
        ''');
        await db.execute('''
          CREATE TABLE order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_order_id TEXT NOT NULL,
            product_pk INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            unit_retail REAL NOT NULL,
            unit_wholesale REAL NOT NULL,
            quantity INTEGER NOT NULL,
            FOREIGN KEY (client_order_id) REFERENCES orders(client_order_id)
              ON DELETE CASCADE
          )
        ''');
      },
    );
    return _database!;
  }

  Future<String> _getOrCreateDatabaseKey() async {
    var key = await _secureStorage.read(key: _databaseKeyName);
    if (key != null) return key;
    final random = Random.secure();
    key = base64UrlEncode(List<int>.generate(32, (_) => random.nextInt(256)));
    await _secureStorage.write(key: _databaseKeyName, value: key);
    return key;
  }

  Future<void> saveCatalog(Map<String, dynamic> catalog) async {
    final snapshotId = catalog['snapshot_id'] as String;
    final products = catalog['products'] as List<dynamic>;
    final db = await database;
    await db.transaction((txn) async {
      await txn.delete('products');
      final batch = txn.batch();
      for (final raw in products) {
        final product = Map<String, dynamic>.from(raw as Map);
        batch.insert('products', {
          'id': product['id'],
          'product_id': product['product_id'] ?? '',
          'product_name': product['product_name'] ?? '',
          'upc': product['upc'] ?? 0,
          'unit': product['unit'] ?? '-',
          'retail': product['retail'] ?? 0,
          'wholesale': product['wholesale'] ?? 0,
          'category': product['category'] ?? 'General',
          'status': product['status'] ?? 'In Stock',
          'description': product['description'] ?? '',
        });
      }
      await batch.commit(noResult: true);
      await txn.insert(
        'catalog_state',
        {
          'id': 1,
          'snapshot_id': snapshotId,
          'fetched_at': DateTime.now().toUtc().toIso8601String(),
        },
        conflictAlgorithm: ConflictAlgorithm.replace,
      );
    });
  }

  Future<Map<String, dynamic>?> getCatalogState() async {
    final db = await database;
    final rows = await db.query('catalog_state', where: 'id = 1');
    return rows.isEmpty ? null : rows.first;
  }

  Future<void> bindToRep(int repId) async {
    final db = await database;
    await db.transaction((txn) async {
      final rows = await txn.query('local_identity', where: 'id = 1');
      if (rows.isNotEmpty && rows.first['rep_id'] != repId) {
        throw StateError(
          'This encrypted database is already assigned to another rep. '
          'Clear this app’s data before enrolling a different rep.',
        );
      }
      if (rows.isEmpty) {
        await txn.insert('local_identity', {'id': 1, 'rep_id': repId});
      }
    });
  }

  Future<List<Map<String, Object?>>> getProducts({String query = ''}) async {
    final db = await database;
    final normalizedQuery = '%${query.trim().toLowerCase()}%';
    return db.rawQuery(
      '''
      SELECT * FROM products
      WHERE LOWER(product_name) LIKE ?
         OR LOWER(product_id) LIKE ?
         OR LOWER(category) LIKE ?
      ORDER BY product_name COLLATE NOCASE
      ''',
      [normalizedQuery, normalizedQuery, normalizedQuery],
    );
  }

  Future<List<Map<String, Object?>>> getOrders() async {
    final db = await database;
    return db.query('orders', orderBy: 'collected_at DESC');
  }

  Future<void> createOrder({
    required String clientOrderId,
    required String outletName,
    required String catalogSnapshotId,
    required List<Map<String, dynamic>> items,
  }) async {
    final db = await database;
    await db.transaction((txn) async {
      await txn.insert('orders', {
        'client_order_id': clientOrderId,
        'outlet_name': outletName.trim(),
        'collected_at': DateTime.now().toUtc().toIso8601String(),
        'catalog_snapshot_id': catalogSnapshotId,
        'status': 'queued',
      });
      final batch = txn.batch();
      for (final item in items) {
        batch.insert('order_items', {
          'client_order_id': clientOrderId,
          'product_pk': item['id'],
          'product_id': item['product_id'],
          'product_name': item['product_name'],
          'unit_retail': item['retail'],
          'unit_wholesale': item['wholesale'],
          'quantity': item['quantity'],
        });
      }
      await batch.commit(noResult: true);
    });
  }

  Future<List<Map<String, dynamic>>> getQueuedOrders() async {
    final db = await database;
    final orders = await db.query(
      'orders',
      where: 'status = ?',
      whereArgs: ['queued'],
      orderBy: 'collected_at ASC',
      limit: 50,
    );
    final result = <Map<String, dynamic>>[];
    for (final order in orders) {
      final items = await db.query(
        'order_items',
        where: 'client_order_id = ?',
        whereArgs: [order['client_order_id']],
        orderBy: 'id ASC',
      );
      result.add({
        'client_order_id': order['client_order_id'],
        'outlet_name': order['outlet_name'],
        'collected_at': order['collected_at'],
        'catalog_snapshot_id': order['catalog_snapshot_id'],
        'items': items
            .map((item) => {
                  'product_pk': item['product_pk'],
                  'quantity': item['quantity'],
                })
            .toList(),
      });
    }
    return result;
  }

  Future<void> applySyncResults(List<dynamic> results) async {
    final db = await database;
    await db.transaction((txn) async {
      for (final raw in results) {
        final result = Map<String, dynamic>.from(raw as Map);
        final clientOrderId = result['client_order_id']?.toString();
        if (clientOrderId == null) continue;
        if (result['ok'] == true) {
          await txn.update(
            'orders',
            {
              'status': result['price_mismatch'] == true ? 'review' : 'synced',
              'server_order_id': result['order_id'],
              'price_mismatch': result['price_mismatch'] == true ? 1 : 0,
              'sync_error': null,
            },
            where: 'client_order_id = ?',
            whereArgs: [clientOrderId],
          );
        } else {
          await txn.update(
            'orders',
            {'sync_error': result['error']?.toString() ?? 'Sync failed.'},
            where: 'client_order_id = ? AND status = ?',
            whereArgs: [clientOrderId, 'queued'],
          );
        }
      }
    });
  }

  Future<List<Map<String, Object?>>> getOrderItems(String clientOrderId) async {
    final db = await database;
    return db.query(
      'order_items',
      where: 'client_order_id = ?',
      whereArgs: [clientOrderId],
      orderBy: 'id ASC',
    );
  }

  Future<void> close() async {
    await _database?.close();
    _database = null;
  }
}
