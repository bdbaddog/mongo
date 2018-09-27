// Tests mongos behavior on stale database version errors received in a transaction.
//
// @tags: [requires_sharding, uses_transactions]
(function() {
    "use strict";

    const dbName = "test";
    const collName = "foo";

    const st = new ShardingTest({shards: 2, mongos: 1, config: 1});

    // Set up two unsharded collections in different databases with shard0 as their primary.

    assert.writeOK(st.s.getDB(dbName)[collName].insert({_id: 0}, {writeConcern: {w: "majority"}}));
    assert.commandWorked(st.s.adminCommand({enableSharding: dbName}));
    st.ensurePrimaryShard(dbName, st.shard0.shardName);

    const session = st.s.startSession();
    const sessionDB = session.getDatabase(dbName);

    //
    // Stale database version on first overall command should succeed.
    //

    session.startTransaction();

    // No database versioned requests have been sent to Shard0, so it is stale.
    assert.commandWorked(sessionDB.runCommand({distinct: collName, key: "_id", query: {_id: 0}}));

    // TODO SERVER-36304: Change this to commitTransaction once multi shard transactions can be
    // committed through mongos.
    session.abortTransaction();

    //
    // Stale database version on second command to a shard should fail.
    //

    st.ensurePrimaryShard(dbName, st.shard1.shardName);

    session.startTransaction();

    // Find is not database versioned so it will not trigger SDV or a refresh on Shard0.
    assert.commandWorked(sessionDB.runCommand({find: collName, filter: {_id: 0}}));

    // Distinct is database versioned, so it will trigger SDV. The router will retry and the retry
    // will discover the transaction was aborted, because a previous statement had completed on
    // Shard0.
    let res = assert.commandFailedWithCode(
        sessionDB.runCommand({distinct: collName, key: "_id", query: {_id: 0}}),
        ErrorCodes.NoSuchTransaction);
    assert.eq(res.errorLabels, ["TransientTransactionError"]);

    session.abortTransaction();

    //
    // Stale database version on first command to a new shard should succeed.
    //

    // Create a new database on Shard0.
    const otherDbName = "other_test";
    const otherCollName = "bar";

    assert.writeOK(
        st.s.getDB(otherDbName)[otherCollName].insert({_id: 0}, {writeConcern: {w: "majority"}}));
    assert.commandWorked(st.s.adminCommand({enableSharding: otherDbName}));
    st.ensurePrimaryShard(otherDbName, st.shard0.shardName);

    const sessionOtherDB = session.getDatabase(otherDbName);

    // Advance the router's cached last committed opTime for Shard0, so it chooses a read timestamp
    // after the collection is created on shard1, to avoid SnapshotUnavailable.
    assert.commandWorked(
        sessionOtherDB.runCommand({find: otherCollName}));  // Not database versioned.
    assert.writeOK(sessionDB[collName].insert({_id: 1}, {writeConcern: {w: "majority"}}));

    session.startTransaction();

    // Target the first database which is on Shard1.
    assert.commandWorked(sessionDB.runCommand({distinct: collName, key: "_id", query: {_id: 0}}));

    // Targets the new database on Shard0 which is stale, so a database versioned request should
    // trigger SDV.
    assert.commandWorked(
        sessionOtherDB.runCommand({distinct: otherCollName, key: "_id", query: {_id: 0}}));

    // TODO SERVER-36304: Change this to commitTransaction.
    session.abortTransaction();

    //
    // NoSuchTransaction should be returned if the router exhausts its retries.
    //

    st.ensurePrimaryShard(dbName, st.shard0.shardName);

    // Disable database metadata refreshes on the stale shard so it will indefinitely return a stale
    // version error.
    assert.commandWorked(st.rs0.getPrimary().adminCommand(
        {configureFailPoint: "skipDatabaseVersionMetadataRefresh", mode: "alwaysOn"}));

    session.startTransaction();

    // Target the first database which is on Shard0. The shard is stale and won't refresh its
    // metadata, so mongos should exhaust its retries and implicitly abort the transaction.
    assert.commandFailedWithCode(
        sessionDB.runCommand({distinct: collName, key: "_id", query: {_id: 0}}),
        ErrorCodes.NoSuchTransaction);

    session.abortTransaction();

    assert.commandWorked(st.rs0.getPrimary().adminCommand(
        {configureFailPoint: "skipDatabaseVersionMetadataRefresh", mode: "off"}));

    st.stop();
})();
