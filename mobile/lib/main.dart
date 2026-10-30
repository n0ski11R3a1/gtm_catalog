import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:local_auth/local_auth.dart';
import 'package:uuid/uuid.dart';

import 'api.dart';
import 'local_store.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const GtmFieldOrdersApp());
}

class GtmFieldOrdersApp extends StatelessWidget {
  const GtmFieldOrdersApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'GTM Field Orders',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xff176b52)),
        useMaterial3: true,
      ),
      home: const FieldOrdersHome(),
    );
  }
}

class FieldOrdersHome extends StatefulWidget {
  const FieldOrdersHome({super.key});

  @override
  State<FieldOrdersHome> createState() => _FieldOrdersHomeState();
}

class _FieldOrdersHomeState extends State<FieldOrdersHome>
    with WidgetsBindingObserver {
  static const _tokenKey = 'gtm_mobile_access_token_v1';
  static const _serverUrlKey = 'gtm_mobile_server_url_v1';
  static const _repKey = 'gtm_mobile_rep_v1';
  static const _secureStorage = FlutterSecureStorage();
  static const _uuid = Uuid();

  final _auth = LocalAuthentication();
  final _usernameController = TextEditingController();
  final _passwordController = TextEditingController();
  final _serverController = TextEditingController();
  final _outletController = TextEditingController();
  final _searchController = TextEditingController();
  final Map<int, int> _cart = {};
  final Map<int, Map<String, Object?>> _cartProducts = {};

  late final LocalStore _store = LocalStore(_secureStorage);
  MobileApi? _api;
  String? _token;
  Map<String, dynamic>? _rep;
  List<Map<String, Object?>> _products = [];
  List<Map<String, Object?>> _orders = [];
  int _tabIndex = 0;
  bool _busy = false;
  bool _locked = true;
  bool _unlockInProgress = false;
  String? _error;
  String? _notice;
  String? _snapshotId;
  String? _catalogFetchedAt;

  bool get _isEnrolled => _token != null && _rep != null && _api != null;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _restoreEnrollment();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _usernameController.dispose();
    _passwordController.dispose();
    _serverController.dispose();
    _outletController.dispose();
    _searchController.dispose();
    _store.close();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.inactive ||
        state == AppLifecycleState.paused ||
        state == AppLifecycleState.hidden) {
      if (_isEnrolled && mounted) setState(() => _locked = true);
    } else if (state == AppLifecycleState.resumed && _isEnrolled) {
      _unlock();
    }
  }

  Future<void> _restoreEnrollment() async {
    try {
      final values = await Future.wait([
        _secureStorage.read(key: _tokenKey),
        _secureStorage.read(key: _serverUrlKey),
        _secureStorage.read(key: _repKey),
      ]);
      if (!mounted) return;
      _serverController.text = values[1] ?? '';
      if (values[0] == null || values[1] == null || values[2] == null) {
        setState(() => _locked = false);
        return;
      }

      final rep = jsonDecode(values[2]!) as Map<String, dynamic>;
      await _store.bindToRep(rep['id'] as int);
      setState(() {
        _token = values[0];
        _rep = rep;
        _api = MobileApi(values[1]!);
      });
      await _unlock();
    } catch (error) {
      if (mounted) {
        setState(() {
          _locked = false;
          _error = 'Could not restore this device: $error';
        });
      }
    }
  }

  Future<bool> _authenticateDevice() async {
    try {
      final canCheck = await _auth.isDeviceSupported();
      if (!canCheck) {
        throw StateError('Set a screen lock in Android settings to use this app.');
      }
      return await _auth.authenticate(
        localizedReason: 'Unlock GTM Field Orders with your phone screen lock',
        biometricOnly: false,
        persistAcrossBackgrounding: true,
      );
    } on PlatformException catch (error) {
      if (error.code == 'NotAvailable' || error.code == 'NotEnrolled') {
        throw StateError('Set a screen lock in Android settings to use this app.');
      }
      rethrow;
    }
  }

  Future<void> _unlock() async {
    if (_unlockInProgress || !_isEnrolled) return;
    _unlockInProgress = true;
    try {
      final authenticated = await _authenticateDevice();
      if (!mounted) return;
      if (!authenticated) {
        setState(() {
          _locked = true;
          _error = 'Unlock with your phone screen lock to continue.';
        });
        return;
      }
      setState(() {
        _locked = false;
        _error = null;
      });
      await _loadLocalData();
    } catch (error) {
      if (mounted) {
        setState(() {
          _locked = true;
          _error = error.toString();
        });
      }
    } finally {
      _unlockInProgress = false;
    }
  }

  Future<void> _enroll() async {
    final serverUrl = _serverController.text.trim().replaceAll(RegExp(r'/+$'), '');
    final username = _usernameController.text.trim();
    final password = _passwordController.text;
    if (serverUrl.isEmpty || username.isEmpty || password.isEmpty) {
      setState(() => _error = 'Enter server URL, username, and password.');
      return;
    }
    final uri = Uri.tryParse(serverUrl);
    if (uri == null || uri.scheme != 'https' || uri.host.isEmpty) {
      setState(() => _error = 'Use the secure https:// address of your catalog server.');
      return;
    }

    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    try {
      final api = MobileApi(serverUrl);
      final response = await api.login(username, password);
      final rep = Map<String, dynamic>.from(response['rep'] as Map);
      final token = response['access_token'] as String;

      if (!await _authenticateDevice()) {
        throw StateError('Phone screen-lock verification is required to enroll.');
      }

      await _store.bindToRep(rep['id'] as int);
      await _secureStorage.write(key: _tokenKey, value: token);
      await _secureStorage.write(key: _serverUrlKey, value: serverUrl);
      await _secureStorage.write(key: _repKey, value: jsonEncode(rep));
      setState(() {
        _token = token;
        _rep = rep;
        _api = api;
        _locked = false;
      });
      await _refreshCatalog();
      await _loadLocalData();
    } catch (error) {
      setState(() => _error = error.toString());
    } finally {
      if (mounted) setState(() => _busy = false);
      _passwordController.clear();
    }
  }

  Future<void> _loadLocalData() async {
    final state = await _store.getCatalogState();
    final products = await _store.getProducts(query: _searchController.text);
    final orders = await _store.getOrders();
    if (!mounted) return;
    setState(() {
      _snapshotId = state?['snapshot_id'] as String?;
      _catalogFetchedAt = state?['fetched_at'] as String?;
      _products = products;
      _orders = orders;
    });
  }

  Future<void> _refreshCatalog() async {
    final api = _api;
    final token = _token;
    if (api == null || token == null) return;
    final catalog = await api.fetchCatalog(token);
    await _store.saveCatalog(catalog);
    _snapshotId = catalog['snapshot_id'] as String;
  }

  Future<void> _sync() async {
    final api = _api;
    final token = _token;
    if (api == null || token == null) return;
    if (_cart.isNotEmpty) {
      setState(() => _error = 'Save or clear the current cart before syncing the catalog.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    try {
      var uploaded = 0;
      var hasFailedOrders = false;
      while (true) {
        final queued = await _store.getQueuedOrders();
        if (queued.isEmpty) break;
        final results = await api.syncOrders(token, queued);
        if (results.length != queued.length) {
          throw const FormatException('The server returned an incomplete sync response.');
        }
        final expectedIds = queued
            .map((order) => order['client_order_id'] as String)
            .toSet();
        final resultIds = results
            .map((result) =>
                (result as Map<String, dynamic>)['client_order_id']?.toString())
            .toList();
        if (resultIds.toSet().length != results.length ||
            resultIds.any((id) => id == null || !expectedIds.contains(id)) ||
            resultIds.length != expectedIds.length) {
          throw const FormatException('The server returned mismatched sync results.');
        }
        await _store.applySyncResults(results);
        uploaded += results.where((result) => result['ok'] == true).length;
        final failed = results.where((result) => result['ok'] != true).length;
        if (failed > 0) {
          hasFailedOrders = true;
          break;
        }
      }
      await _refreshCatalog();
      await _loadLocalData();
      if (mounted) {
        setState(() {
          _notice = hasFailedOrders
              ? 'Catalog refreshed. $uploaded order(s) uploaded; some orders need attention before retrying.'
              : 'Sync complete. $uploaded order(s) uploaded; catalog refreshed.';
        });
      }
    } on MobileApiException catch (error) {
      if (error.statusCode == 401) {
        await _secureStorage.delete(key: _tokenKey);
        if (mounted) {
          setState(() {
            _token = null;
            _api = null;
            _locked = false;
            _error =
                'This phone is no longer authorized. Sign in again; saved orders remain on this phone.';
          });
        }
      } else if (mounted) {
        setState(() => _error = error.message);
      }
    } catch (error) {
      if (mounted) setState(() => _error = 'Sync failed; queued orders remain saved: $error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _saveOrder() async {
    final outlet = _outletController.text.trim();
    if (outlet.isEmpty || _cart.isEmpty || _snapshotId == null) {
      setState(() => _error =
          'Enter an outlet, add products, and sync the catalog before collecting orders.');
      return;
    }
    final items = <Map<String, dynamic>>[];
    for (final entry in _cart.entries) {
      final product = _cartProducts[entry.key];
      if (product == null) continue;
      items.add({...product, 'quantity': entry.value});
    }
    if (items.isEmpty) {
      setState(() => _error = 'No products from the current catalog are selected.');
      return;
    }
    try {
      await _store.createOrder(
        clientOrderId: _uuid.v4(),
        outletName: outlet,
        catalogSnapshotId: _snapshotId!,
        items: items,
      );
      _cart.clear();
      _cartProducts.clear();
      _outletController.clear();
      await _loadLocalData();
      if (mounted) {
        setState(() {
          _tabIndex = 1;
          _notice = 'Order saved securely on this phone. Sync it when online.';
          _error = null;
        });
      }
    } catch (error) {
      if (mounted) setState(() => _error = 'Could not save order: $error');
    }
  }

  Future<void> _search(String query) async {
    final products = await _store.getProducts(query: query);
    if (mounted) setState(() => _products = products);
  }

  @override
  Widget build(BuildContext context) {
    if (_isEnrolled && _locked) return _buildLockedScreen();
    if (!_isEnrolled) return _buildLoginScreen();
    return _buildMainScreen();
  }

  Widget _buildLoginScreen() {
    return Scaffold(
      appBar: AppBar(title: const Text('GTM Field Orders')),
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 480),
            child: ListView(
              padding: const EdgeInsets.all(24),
              shrinkWrap: true,
              children: [
                const Icon(Icons.inventory_2_outlined, size: 56),
                const SizedBox(height: 20),
                const Text(
                  'Sign in while online to enroll this phone. After that, orders can be collected offline.',
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 24),
                TextField(
                  controller: _serverController,
                  keyboardType: TextInputType.url,
                  decoration: const InputDecoration(
                    labelText: 'Secure server URL',
                    hintText: 'https://catalog.example.com',
                  ),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _usernameController,
                  autocorrect: false,
                  decoration: const InputDecoration(labelText: 'Rep username'),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _passwordController,
                  obscureText: true,
                  decoration: const InputDecoration(labelText: 'Password'),
                  onSubmitted: (_) => _busy ? null : _enroll(),
                ),
                const SizedBox(height: 20),
                FilledButton.icon(
                  onPressed: _busy ? null : _enroll,
                  icon: _busy
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.lock_open),
                  label: Text(_busy ? 'Signing in…' : 'Sign in and enroll phone'),
                ),
                if (_error != null) ...[
                  const SizedBox(height: 16),
                  Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildLockedScreen() {
    return Scaffold(
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.lock_outline, size: 56),
              const SizedBox(height: 16),
              const Text('GTM Field Orders is locked'),
              if (_error != null) ...[
                const SizedBox(height: 12),
                Text(_error!, textAlign: TextAlign.center),
              ],
              const SizedBox(height: 20),
              FilledButton.icon(
                onPressed: _unlock,
                icon: const Icon(Icons.phonelink_lock),
                label: const Text('Unlock with phone screen lock'),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildMainScreen() {
    final repName = '${_rep?['code'] ?? ''} (${_rep?['name'] ?? ''})';
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(_tabIndex == 0 ? 'Catalog' : 'Orders'),
            Text(repName, style: Theme.of(context).textTheme.labelSmall),
          ],
        ),
        actions: [
          IconButton(
            onPressed: _busy ? null : _sync,
            icon: _busy
                ? const SizedBox(
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.sync),
            tooltip: 'Sync orders and refresh catalog',
          ),
        ],
      ),
      body: Column(
        children: [
          if (_error != null)
            MaterialBanner(
              content: Text(_error!),
              actions: [
                TextButton(
                  onPressed: () => setState(() => _error = null),
                  child: const Text('Dismiss'),
                ),
              ],
            ),
          if (_notice != null)
            MaterialBanner(
              content: Text(_notice!),
              actions: [
                TextButton(
                  onPressed: () => setState(() => _notice = null),
                  child: const Text('Dismiss'),
                ),
              ],
            ),
          if (_catalogFetchedAt != null)
            ListTile(
              dense: true,
              leading: const Icon(Icons.cloud_done_outlined),
              title: Text(
                'Offline catalog · ${DateTime.parse(_catalogFetchedAt!).toLocal()}',
                style: Theme.of(context).textTheme.bodySmall,
              ),
              trailing: TextButton(
                onPressed: _busy ? null : _sync,
                child: const Text('Sync now'),
              ),
            ),
          Expanded(child: _tabIndex == 0 ? _buildCatalog() : _buildOrders()),
        ],
      ),
      floatingActionButton: _tabIndex == 0 && _cart.isNotEmpty
          ? FloatingActionButton.extended(
              onPressed: _saveOrder,
              icon: const Icon(Icons.save_alt),
              label: Text('Save order (${_cart.length})'),
            )
          : null,
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tabIndex,
        onDestinationSelected: (index) {
          setState(() => _tabIndex = index);
          if (index == 1) _loadLocalData();
        },
        destinations: const [
          NavigationDestination(icon: Icon(Icons.storefront_outlined), label: 'Catalog'),
          NavigationDestination(icon: Icon(Icons.receipt_long_outlined), label: 'Orders'),
        ],
      ),
    );
  }

  Widget _buildCatalog() {
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
          child: TextField(
            controller: _searchController,
            onChanged: _search,
            decoration: InputDecoration(
              prefixIcon: const Icon(Icons.search),
              hintText: 'Search name, Product ID, category',
              suffixIcon: IconButton(
                onPressed: () {
                  _searchController.clear();
                  _search('');
                },
                icon: const Icon(Icons.clear),
              ),
            ),
          ),
        ),
        Expanded(
          child: _products.isEmpty
              ? const Center(
                  child: Text('No offline catalog yet. Connect and tap Sync now.'),
                )
              : ListView.builder(
                  itemCount: _products.length,
                  itemBuilder: (context, index) {
                    final product = _products[index];
                    final id = product['id'] as int;
                    final quantity = _cart[id] ?? 0;
                    final outOfStock =
                        (product['status'] as String).toLowerCase() == 'out of stock';
                    return Card(
                      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
                      child: ListTile(
                        title: Text(product['product_name'] as String),
                        subtitle: Text(
                          '${product['product_id']} · ${product['unit']} · '
                          'Retail ${_money(product['retail'])} Ks · '
                          'Wholesale ${_money(product['wholesale'])} Ks'
                          '${outOfStock ? ' · OUT OF STOCK' : ''}',
                        ),
                        isThreeLine: true,
                        trailing: Wrap(
                          crossAxisAlignment: WrapCrossAlignment.center,
                          children: [
                            IconButton(
                              onPressed: quantity == 0
                                  ? null
                                  : () => setState(() {
                                        if (quantity == 1) {
                                          _cart.remove(id);
                                        } else {
                                          _cart[id] = quantity - 1;
                                        }
                                      }),
                              icon: const Icon(Icons.remove_circle_outline),
                            ),
                            Text('$quantity'),
                            IconButton(
                              onPressed: outOfStock
                                  ? null
                                  : () => setState(() {
                                        _cart[id] = quantity + 1;
                                        _cartProducts[id] = product;
                                      }),
                              icon: const Icon(Icons.add_circle_outline),
                            ),
                          ],
                        ),
                      ),
                    );
                  },
                ),
        ),
        if (_cart.isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
            child: TextField(
              controller: _outletController,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(
                labelText: 'Outlet name',
                prefixIcon: Icon(Icons.store),
              ),
            ),
          ),
      ],
    );
  }

  Widget _buildOrders() {
    if (_orders.isEmpty) {
      return const Center(child: Text('No orders saved on this phone yet.'));
    }
    return ListView.builder(
      padding: const EdgeInsets.only(bottom: 96),
      itemCount: _orders.length,
      itemBuilder: (context, index) {
        final order = _orders[index];
        final status = order['status'] as String;
        final mismatch = order['price_mismatch'] == 1;
        return Card(
          margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
          child: ExpansionTile(
            leading: Icon(
              mismatch ? Icons.warning_amber : status == 'queued' ? Icons.schedule : Icons.check_circle,
              color: mismatch
                  ? Colors.orange
                  : status == 'queued'
                      ? Colors.blueGrey
                      : Colors.green,
            ),
            title: Text(order['outlet_name'] as String),
            subtitle: Text(
              '${DateTime.parse(order['collected_at'] as String).toLocal()} · '
              '${mismatch ? 'PRICE REVIEW' : status.toUpperCase()}'
              '${order['server_order_id'] == null ? '' : ' · Server #${order['server_order_id']}'}',
            ),
            children: [
              FutureBuilder<List<Map<String, Object?>>>(
                future: _store.getOrderItems(order['client_order_id'] as String),
                builder: (context, snapshot) {
                  final items = snapshot.data ?? const [];
                  return Column(
                    children: items
                        .map(
                          (item) => ListTile(
                            dense: true,
                            title: Text(item['product_name'] as String),
                            subtitle: Text(item['product_id'] as String),
                            trailing: Text(
                              '${item['quantity']} × ${_money(item['unit_retail'])} Ks',
                            ),
                          ),
                        )
                        .toList(),
                  );
                },
              ),
              if (order['sync_error'] != null)
                Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(
                    order['sync_error'] as String,
                    style: TextStyle(color: Theme.of(context).colorScheme.error),
                  ),
                ),
            ],
          ),
        );
      },
    );
  }

  String _money(Object? value) {
    final number = (value as num?)?.toDouble() ?? 0;
    return number.toStringAsFixed(number.truncateToDouble() == number ? 0 : 2);
  }
}
