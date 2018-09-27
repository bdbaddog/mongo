// Cannot implicitly shard accessed collections because of not being able to create unique index
// using hashed shard key pattern.
// @tags: [cannot_create_unique_index_when_using_hashed_shard_key]

t = db.indexapi;
t.drop();

key = {
    x: 1
};

c = {
    ns: t._fullName,
    key: key,
    name: t._genIndexName(key)
};
assert.eq(c, t._indexSpec({x: 1}), "A");

c.name = "bob";
assert.eq(c, t._indexSpec({x: 1}, "bob"), "B");

c.name = t._genIndexName(key);
assert.eq(c, t._indexSpec({x: 1}), "C");

c.unique = true;
assert.eq(c, t._indexSpec({x: 1}, true), "D");
assert.eq(c, t._indexSpec({x: 1}, [true]), "E");
assert.eq(c, t._indexSpec({x: 1}, {unique: true}), "F");

c.dropDups = true;
assert.eq(c, t._indexSpec({x: 1}, [true, true]), "G");
assert.eq(c, t._indexSpec({x: 1}, {unique: true, dropDups: true}), "F");

t.ensureIndex({x: 1}, {unique: true});
idx = t.getIndexes();
assert.eq(2, idx.length, "M1");
assert.eq(key, idx[1].key, "M2");
assert(idx[1].unique, "M3");

t.drop();
t.ensureIndex({x: 1}, {unique: 1});
idx = t.getIndexes();
assert.eq(2, idx.length, "M1");
assert.eq(key, idx[1].key, "M2");
assert(idx[1].unique, "M3");
