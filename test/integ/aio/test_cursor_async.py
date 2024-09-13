#!/usr/bin/env python
#
# Copyright (c) 2012-2023 Snowflake Computing Inc. All rights reserved.
#

from __future__ import annotations

import asyncio
import decimal
import json
import logging
import os
import pickle
import time
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, NamedTuple
from unittest import mock

import pytest
import pytz

import snowflake.connector
from snowflake.connector import (
    InterfaceError,
    NotSupportedError,
    ProgrammingError,
    connection,
    constants,
    errorcode,
    errors,
)
from snowflake.connector.aio import DictCursor, SnowflakeCursor
from snowflake.connector.compat import IS_WINDOWS

try:
    from snowflake.connector.cursor import ResultMetadata
except ImportError:

    class ResultMetadata(NamedTuple):
        name: str
        type_code: int
        display_size: int
        internal_size: int
        precision: int
        scale: int
        is_nullable: bool


import snowflake.connector.aio
from snowflake.connector.description import CLIENT_VERSION
from snowflake.connector.errorcode import (
    ER_FAILED_TO_REWRITE_MULTI_ROW_INSERT,
    ER_NOT_POSITIVE_SIZE,
)
from snowflake.connector.errors import Error
from snowflake.connector.sqlstate import SQLSTATE_FEATURE_NOT_SUPPORTED
from snowflake.connector.telemetry import TelemetryField

try:
    from snowflake.connector.util_text import random_string
except ImportError:
    from ..randomize import random_string

try:
    from snowflake.connector.aio._result_batch import ArrowResultBatch, JSONResultBatch
    from snowflake.connector.constants import (
        FIELD_ID_TO_NAME,
        PARAMETER_MULTI_STATEMENT_COUNT,
        PARAMETER_PYTHON_CONNECTOR_QUERY_RESULT_FORMAT,
    )
    from snowflake.connector.errorcode import (
        ER_NO_ARROW_RESULT,
        ER_NO_PYARROW,
        ER_NO_PYARROW_SNOWSQL,
    )
except ImportError:
    PARAMETER_PYTHON_CONNECTOR_QUERY_RESULT_FORMAT = None
    ER_NO_ARROW_RESULT = None
    ER_NO_PYARROW = None
    ER_NO_PYARROW_SNOWSQL = None
    ArrowResultBatch = JSONResultBatch = None
    FIELD_ID_TO_NAME = {}

if TYPE_CHECKING:  # pragma: no cover
    from snowflake.connector.result_batch import ResultBatch

try:  # pragma: no cover
    from snowflake.connector.constants import QueryStatus
except ImportError:
    QueryStatus = None


@pytest.fixture
async def conn(conn_cnx, db_parameters):
    async with conn_cnx() as cnx:
        await cnx.cursor().execute(
            """
create table {name} (
aa int,
dt date,
tm time,
ts timestamp,
tsltz timestamp_ltz,
tsntz timestamp_ntz,
tstz timestamp_tz,
pct float,
ratio number(5,2),
b binary)
""".format(
                name=db_parameters["name"]
            )
        )

    yield conn_cnx

    async with conn_cnx() as cnx:
        await cnx.cursor().execute(
            "use {db}.{schema}".format(
                db=db_parameters["database"], schema=db_parameters["schema"]
            )
        )
        await cnx.cursor().execute(
            "drop table {name}".format(name=db_parameters["name"])
        )


def _check_results(cursor, results):
    assert cursor.sfqid, "Snowflake query id is None"
    assert cursor.rowcount == 3, "the number of records"
    assert results[0] == 65432, "the first result was wrong"
    assert results[1] == 98765, "the second result was wrong"
    assert results[2] == 123456, "the third result was wrong"


def _name_from_description(named_access: bool):
    if named_access:
        return lambda meta: meta.name
    else:
        return lambda meta: meta[0]


def _type_from_description(named_access: bool):
    if named_access:
        return lambda meta: meta.type_code
    else:
        return lambda meta: meta[1]


async def test_insert_select(conn, db_parameters, caplog):
    """Inserts and selects integer data."""
    async with conn() as cnx:
        c = cnx.cursor()
        try:
            await c.execute(
                "insert into {name}(aa) values(123456),"
                "(98765),(65432)".format(name=db_parameters["name"])
            )
            cnt = 0
            async for rec in c:
                cnt += int(rec[0])
            assert cnt == 3, "wrong number of records were inserted"
            assert c.rowcount == 3, "wrong number of records were inserted"
        finally:
            await c.close()

        try:
            c = cnx.cursor()
            await c.execute(
                "select aa from {name} order by aa".format(name=db_parameters["name"])
            )
            results = []
            async for rec in c:
                results.append(rec[0])
            _check_results(c, results)
            assert "Number of results in first chunk: 3" in caplog.text
        finally:
            await c.close()

        async with cnx.cursor(snowflake.connector.aio.DictCursor) as c:
            caplog.clear()
            assert "Number of results in first chunk: 3" not in caplog.text
            await c.execute(
                "select aa from {name} order by aa".format(name=db_parameters["name"])
            )
            results = []
            async for rec in c:
                results.append(rec["AA"])
            _check_results(c, results)
            assert "Number of results in first chunk: 3" in caplog.text


async def test_insert_and_select_by_separate_connection(conn, db_parameters, caplog):
    """Inserts a record and select it by a separate connection."""
    async with conn() as cnx:
        result = await cnx.cursor().execute(
            "insert into {name}(aa) values({value})".format(
                name=db_parameters["name"], value="1234"
            )
        )
        cnt = 0
        async for rec in result:
            cnt += int(rec[0])
        assert cnt == 1, "wrong number of records were inserted"
        assert result.rowcount == 1, "wrong number of records were inserted"

    cnx2 = snowflake.connector.aio.SnowflakeConnection(
        user=db_parameters["user"],
        password=db_parameters["password"],
        host=db_parameters["host"],
        port=db_parameters["port"],
        account=db_parameters["account"],
        database=db_parameters["database"],
        schema=db_parameters["schema"],
        protocol=db_parameters["protocol"],
        timezone="UTC",
    )
    await cnx2.connect()
    try:
        c = cnx2.cursor()
        await c.execute("select aa from {name}".format(name=db_parameters["name"]))
        results = []
        async for rec in c:
            results.append(rec[0])
        await c.close()
        assert results[0] == 1234, "the first result was wrong"
        assert result.rowcount == 1, "wrong number of records were selected"
        assert "Number of results in first chunk: 1" in caplog.text
    finally:
        await cnx2.close()


def _total_milliseconds_from_timedelta(td):
    """Returns the total number of milliseconds contained in the duration object."""
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) // 10**3


def _total_seconds_from_timedelta(td):
    """Returns the total number of seconds contained in the duration object."""
    return _total_milliseconds_from_timedelta(td) // 10**3


async def test_insert_timestamp_select(conn, db_parameters):
    """Inserts and gets timestamp, timestamp with tz, date, and time.

    Notes:
        Currently the session parameter TIMEZONE is ignored.
    """
    PST_TZ = "America/Los_Angeles"
    JST_TZ = "Asia/Tokyo"
    current_timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    current_timestamp = current_timestamp.replace(tzinfo=pytz.timezone(PST_TZ))
    current_date = current_timestamp.date()
    current_time = current_timestamp.time()

    other_timestamp = current_timestamp.replace(tzinfo=pytz.timezone(JST_TZ))

    async with conn() as cnx:
        await cnx.cursor().execute("alter session set TIMEZONE=%s", (PST_TZ,))
        c = cnx.cursor()
        try:
            fmt = (
                "insert into {name}(aa, tsltz, tstz, tsntz, dt, tm) "
                "values(%(value)s,%(tsltz)s, %(tstz)s, %(tsntz)s, "
                "%(dt)s, %(tm)s)"
            )
            await c.execute(
                fmt.format(name=db_parameters["name"]),
                {
                    "value": 1234,
                    "tsltz": current_timestamp,
                    "tstz": other_timestamp,
                    "tsntz": current_timestamp,
                    "dt": current_date,
                    "tm": current_time,
                },
            )
            cnt = 0
            async for rec in c:
                cnt += int(rec[0])
            assert cnt == 1, "wrong number of records were inserted"
            assert c.rowcount == 1, "wrong number of records were selected"
        finally:
            await c.close()

    cnx2 = snowflake.connector.aio.SnowflakeConnection(
        user=db_parameters["user"],
        password=db_parameters["password"],
        host=db_parameters["host"],
        port=db_parameters["port"],
        account=db_parameters["account"],
        database=db_parameters["database"],
        schema=db_parameters["schema"],
        protocol=db_parameters["protocol"],
        timezone="UTC",
    )
    await cnx2.connect()
    try:
        c = cnx2.cursor()
        await c.execute(
            "select aa, tsltz, tstz, tsntz, dt, tm from {name}".format(
                name=db_parameters["name"]
            )
        )

        result_numeric_value = []
        result_timestamp_value = []
        result_other_timestamp_value = []
        result_ntz_timestamp_value = []
        result_date_value = []
        result_time_value = []

        async for aa, ts, tstz, tsntz, dt, tm in c:
            result_numeric_value.append(aa)
            result_timestamp_value.append(ts)
            result_other_timestamp_value.append(tstz)
            result_ntz_timestamp_value.append(tsntz)
            result_date_value.append(dt)
            result_time_value.append(tm)
        await c.close()
        assert result_numeric_value[0] == 1234, "the integer result was wrong"

        td_diff = _total_milliseconds_from_timedelta(
            current_timestamp - result_timestamp_value[0]
        )
        assert td_diff == 0, "the timestamp result was wrong"

        td_diff = _total_milliseconds_from_timedelta(
            other_timestamp - result_other_timestamp_value[0]
        )
        assert td_diff == 0, "the other timestamp result was wrong"

        td_diff = _total_milliseconds_from_timedelta(
            current_timestamp.replace(tzinfo=None) - result_ntz_timestamp_value[0]
        )
        assert td_diff == 0, "the other timestamp result was wrong"

        assert current_date == result_date_value[0], "the date result was wrong"

        assert current_time == result_time_value[0], "the time result was wrong"

        name = _name_from_description(False)
        type_code = _type_from_description(False)
        descriptions = [c.description]
        if hasattr(c, "_description_internal"):
            # If _description_internal is defined, even the old description attribute will
            # return ResultMetadata (v1) and not a plain tuple. This indirection is needed
            # to support old-driver tests
            name = _name_from_description(True)
            type_code = _type_from_description(True)
            descriptions.append(c._description_internal)
        for desc in descriptions:
            assert len(desc) == 6, "invalid number of column meta data"
            assert name(desc[0]).upper() == "AA", "invalid column name"
            assert name(desc[1]).upper() == "TSLTZ", "invalid column name"
            assert name(desc[2]).upper() == "TSTZ", "invalid column name"
            assert name(desc[3]).upper() == "TSNTZ", "invalid column name"
            assert name(desc[4]).upper() == "DT", "invalid column name"
            assert name(desc[5]).upper() == "TM", "invalid column name"
            assert (
                constants.FIELD_ID_TO_NAME[type_code(desc[0])] == "FIXED"
            ), f"invalid column name: {constants.FIELD_ID_TO_NAME[desc[0][1]]}"
            assert (
                constants.FIELD_ID_TO_NAME[type_code(desc[1])] == "TIMESTAMP_LTZ"
            ), "invalid column name"
            assert (
                constants.FIELD_ID_TO_NAME[type_code(desc[2])] == "TIMESTAMP_TZ"
            ), "invalid column name"
            assert (
                constants.FIELD_ID_TO_NAME[type_code(desc[3])] == "TIMESTAMP_NTZ"
            ), "invalid column name"
            assert (
                constants.FIELD_ID_TO_NAME[type_code(desc[4])] == "DATE"
            ), "invalid column name"
            assert (
                constants.FIELD_ID_TO_NAME[type_code(desc[5])] == "TIME"
            ), "invalid column name"
    finally:
        await cnx2.close()


async def test_insert_timestamp_ltz(conn, db_parameters):
    """Inserts and retrieve timestamp ltz."""
    tzstr = "America/New_York"
    # sync with the session parameter
    async with conn() as cnx:
        await cnx.cursor().execute(f"alter session set timezone='{tzstr}'")

        current_time = datetime.now()
        current_time = current_time.replace(tzinfo=pytz.timezone(tzstr))

        c = cnx.cursor()
        try:
            fmt = "insert into {name}(aa, tsltz) values(%(value)s,%(ts)s)"
            await c.execute(
                fmt.format(name=db_parameters["name"]),
                {
                    "value": 8765,
                    "ts": current_time,
                },
            )
            cnt = 0
            async for rec in c:
                cnt += int(rec[0])
            assert cnt == 1, "wrong number of records were inserted"
        finally:
            await c.close()

        try:
            c = cnx.cursor()
            await c.execute(
                "select aa,tsltz from {name}".format(name=db_parameters["name"])
            )
            result_numeric_value = []
            result_timestamp_value = []
            async for aa, ts in c:
                result_numeric_value.append(aa)
                result_timestamp_value.append(ts)

            td_diff = _total_milliseconds_from_timedelta(
                current_time - result_timestamp_value[0]
            )

            assert td_diff == 0, "the first result was wrong"
        finally:
            await c.close()


async def test_struct_time(conn, db_parameters):
    """Binds struct_time object for updating timestamp."""
    tzstr = "America/New_York"
    os.environ["TZ"] = tzstr
    if not IS_WINDOWS:
        time.tzset()
    test_time = time.strptime("30 Sep 01 11:20:30", "%d %b %y %H:%M:%S")

    async with conn() as cnx:
        c = cnx.cursor()
        try:
            fmt = "insert into {name}(aa, tsltz) values(%(value)s,%(ts)s)"
            await c.execute(
                fmt.format(name=db_parameters["name"]),
                {
                    "value": 87654,
                    "ts": test_time,
                },
            )
            cnt = 0
            async for rec in c:
                cnt += int(rec[0])
        finally:
            c.close()
            os.environ["TZ"] = "UTC"
            if not IS_WINDOWS:
                time.tzset()
        assert cnt == 1, "wrong number of records were inserted"

        try:
            result = await cnx.cursor().execute(
                "select aa, tsltz from {name}".format(name=db_parameters["name"])
            )
            async for _, _tsltz in result:
                pass

            _tsltz -= _tsltz.tzinfo.utcoffset(_tsltz)

            assert test_time.tm_year == _tsltz.year, "Year didn't match"
            assert test_time.tm_mon == _tsltz.month, "Month didn't match"
            assert test_time.tm_mday == _tsltz.day, "Day didn't match"
            assert test_time.tm_hour == _tsltz.hour, "Hour didn't match"
            assert test_time.tm_min == _tsltz.minute, "Minute didn't match"
            assert test_time.tm_sec == _tsltz.second, "Second didn't match"
        finally:
            os.environ["TZ"] = "UTC"
            if not IS_WINDOWS:
                time.tzset()


async def test_insert_binary_select(conn, db_parameters):
    """Inserts and get a binary value."""
    value = b"\x00\xFF\xA1\xB2\xC3"

    async with conn() as cnx:
        c = cnx.cursor()
        try:
            fmt = "insert into {name}(b) values(%(b)s)"
            await c.execute(fmt.format(name=db_parameters["name"]), {"b": value})
            count = sum([int(rec[0]) async for rec in c])
            assert count == 1, "wrong number of records were inserted"
            assert c.rowcount == 1, "wrong number of records were selected"
        finally:
            await c.close()

    cnx2 = snowflake.connector.aio.SnowflakeConnection(
        user=db_parameters["user"],
        password=db_parameters["password"],
        host=db_parameters["host"],
        port=db_parameters["port"],
        account=db_parameters["account"],
        database=db_parameters["database"],
        schema=db_parameters["schema"],
        protocol=db_parameters["protocol"],
    )
    await cnx2.connect()
    try:
        c = cnx2.cursor()
        await c.execute("select b from {name}".format(name=db_parameters["name"]))

        results = [b async for (b,) in c]
        assert value == results[0], "the binary result was wrong"

        name = _name_from_description(False)
        type_code = _type_from_description(False)
        descriptions = [c.description]
        if hasattr(c, "_description_internal"):
            # If _description_internal is defined, even the old description attribute will
            # return ResultMetadata (v1) and not a plain tuple. This indirection is needed
            # to support old-driver tests
            name = _name_from_description(True)
            type_code = _type_from_description(True)
            descriptions.append(c._description_internal)
        for desc in descriptions:
            assert len(desc) == 1, "invalid number of column meta data"
            assert name(desc[0]).upper() == "B", "invalid column name"
            assert (
                constants.FIELD_ID_TO_NAME[type_code(desc[0])] == "BINARY"
            ), "invalid column name"
    finally:
        await cnx2.close()


async def test_insert_binary_select_with_bytearray(conn, db_parameters):
    """Inserts and get a binary value using the bytearray type."""
    value = bytearray(b"\x00\xFF\xA1\xB2\xC3")

    async with conn() as cnx:
        c = cnx.cursor()
        try:
            fmt = "insert into {name}(b) values(%(b)s)"
            await c.execute(fmt.format(name=db_parameters["name"]), {"b": value})
            count = sum([int(rec[0]) async for rec in c])
            assert count == 1, "wrong number of records were inserted"
            assert c.rowcount == 1, "wrong number of records were selected"
        finally:
            c.close()

    cnx2 = snowflake.connector.aio.SnowflakeConnection(
        user=db_parameters["user"],
        password=db_parameters["password"],
        host=db_parameters["host"],
        port=db_parameters["port"],
        account=db_parameters["account"],
        database=db_parameters["database"],
        schema=db_parameters["schema"],
        protocol=db_parameters["protocol"],
    )
    await cnx2.connect()
    try:
        c = cnx2.cursor()
        await c.execute("select b from {name}".format(name=db_parameters["name"]))

        results = [b async for (b,) in c]
        assert bytes(value) == results[0], "the binary result was wrong"

        name = _name_from_description(False)
        type_code = _type_from_description(False)
        descriptions = [c.description]
        if hasattr(c, "_description_internal"):
            # If _description_internal is defined, even the old description attribute will
            # return ResultMetadata (v1) and not a plain tuple. This indirection is needed
            # to support old-driver tests
            name = _name_from_description(True)
            type_code = _type_from_description(True)
            descriptions.append(c._description_internal)
        for desc in descriptions:
            assert len(desc) == 1, "invalid number of column meta data"
            assert name(desc[0]).upper() == "B", "invalid column name"
            assert (
                constants.FIELD_ID_TO_NAME[type_code(desc[0])] == "BINARY"
            ), "invalid column name"
    finally:
        await cnx2.close()


async def test_variant(conn, db_parameters):
    """Variant including JSON object."""
    name_variant = db_parameters["name"] + "_variant"
    async with conn() as cnx:
        await cnx.cursor().execute(
            """
create table {name} (
created_at timestamp, data variant)
""".format(
                name=name_variant
            )
        )

    try:
        async with conn() as cnx:
            current_time = datetime.now()
            c = cnx.cursor()
            try:
                fmt = (
                    "insert into {name}(created_at, data) "
                    "select column1, parse_json(column2) "
                    "from values(%(created_at)s, %(data)s)"
                )
                await c.execute(
                    fmt.format(name=name_variant),
                    {
                        "created_at": current_time,
                        "data": (
                            '{"SESSION-PARAMETERS":{'
                            '"TIMEZONE":"UTC", "SPECIAL_FLAG":true}}'
                        ),
                    },
                )
                cnt = 0
                async for rec in c:
                    cnt += int(rec[0])
                assert cnt == 1, "wrong number of records were inserted"
                assert c.rowcount == 1, "wrong number of records were inserted"
            finally:
                await c.close()

            result = await cnx.cursor().execute(
                f"select created_at, data from {name_variant}"
            )
            _, data = await result.fetchone()
            data = json.loads(data)
            assert data["SESSION-PARAMETERS"]["SPECIAL_FLAG"], (
                "JSON data should be parsed properly. " "Invalid JSON data"
            )
    finally:
        async with conn() as cnx:
            await cnx.cursor().execute(f"drop table {name_variant}")


async def test_geography(conn_cnx):
    """Variant including JSON object."""
    name_geo = random_string(5, "test_geography_")
    async with conn_cnx(
        session_parameters={
            "GEOGRAPHY_OUTPUT_FORMAT": "geoJson",
        },
    ) as cnx:
        async with cnx.cursor() as cur:
            await cur.execute(f"create temporary table {name_geo} (geo geography)")
            await cur.execute(
                f"insert into {name_geo} values ('POINT(0 0)'), ('LINESTRING(1 1, 2 2)')"
            )
            expected_data = [
                {"coordinates": [0, 0], "type": "Point"},
                {"coordinates": [[1, 1], [2, 2]], "type": "LineString"},
            ]

        async with cnx.cursor() as cur:
            # Test with GEOGRAPHY return type
            result = await cur.execute(f"select * from {name_geo}")
            for metadata in [cur.description, cur._description_internal]:
                assert FIELD_ID_TO_NAME[metadata[0].type_code] == "GEOGRAPHY"
            data = await result.fetchall()
            for raw_data in data:
                row = json.loads(raw_data[0])
                assert row in expected_data


async def test_geometry(conn_cnx):
    """Variant including JSON object."""
    name_geo = random_string(5, "test_geometry_")
    async with conn_cnx(
        session_parameters={
            "GEOMETRY_OUTPUT_FORMAT": "geoJson",
        },
    ) as cnx:
        async with cnx.cursor() as cur:
            await cur.execute(f"create temporary table {name_geo} (geo GEOMETRY)")
            await cur.execute(
                f"insert into {name_geo} values ('POINT(0 0)'), ('LINESTRING(1 1, 2 2)')"
            )
            expected_data = [
                {"coordinates": [0, 0], "type": "Point"},
                {"coordinates": [[1, 1], [2, 2]], "type": "LineString"},
            ]

        async with cnx.cursor() as cur:
            # Test with GEOMETRY return type
            result = await cur.execute(f"select * from {name_geo}")
            for metadata in [cur.description, cur._description_internal]:
                assert FIELD_ID_TO_NAME[metadata[0].type_code] == "GEOMETRY"
            data = await result.fetchall()
            for raw_data in data:
                row = json.loads(raw_data[0])
                assert row in expected_data


async def test_vector(conn_cnx, is_public_test):
    if is_public_test:
        pytest.xfail(
            reason="This feature hasn't been rolled out for public Snowflake deployments yet."
        )
    name_vectors = random_string(5, "test_vector_")
    async with conn_cnx() as cnx:
        async with cnx.cursor() as cur:
            # Seed test data
            expected_data_ints = [[1, 3, -5], [40, 1234567, 1], "NULL"]
            expected_data_floats = [
                [1.8, -3.4, 6.7, 0, 2.3],
                [4.121212121, 31234567.4, 7, -2.123, 1],
                "NULL",
            ]
            await cur.execute(
                f"create temporary table {name_vectors} (int_vec VECTOR(INT,3), float_vec VECTOR(FLOAT,5))"
            )
            for i in range(len(expected_data_ints)):
                await cur.execute(
                    f"insert into {name_vectors} select {expected_data_ints[i]}::VECTOR(INT,3), {expected_data_floats[i]}::VECTOR(FLOAT,5)"
                )

        async with cnx.cursor() as cur:
            # Test a basic fetch
            await cur.execute(
                f"select int_vec, float_vec from {name_vectors} order by float_vec"
            )
            for metadata in [cur.description, cur._description_internal]:
                assert FIELD_ID_TO_NAME[metadata[0].type_code] == "VECTOR"
                assert FIELD_ID_TO_NAME[metadata[1].type_code] == "VECTOR"
            data = await cur.fetchall()
            for i, row in enumerate(data):
                if expected_data_floats[i] == "NULL":
                    assert row[0] is None
                else:
                    assert row[0] == expected_data_ints[i]

                if expected_data_ints[i] == "NULL":
                    assert row[1] is None
                else:
                    assert row[1] == pytest.approx(expected_data_floats[i])

            # Test an empty result set
            await cur.execute(
                f"select int_vec, float_vec from {name_vectors} where int_vec = [1,2,3]::VECTOR(int,3)"
            )
            for metadata in [cur.description, cur._description_internal]:
                assert FIELD_ID_TO_NAME[metadata[0].type_code] == "VECTOR"
                assert FIELD_ID_TO_NAME[metadata[1].type_code] == "VECTOR"
            data = await cur.fetchall()
            assert len(data) == 0


async def test_invalid_bind_data_type(conn_cnx):
    """Invalid bind data type."""
    async with conn_cnx() as cnx:
        with pytest.raises(errors.ProgrammingError):
            await cnx.cursor().execute("select 1 from dual where 1=%s", ([1, 2, 3],))


# TODO: SNOW-1657469 for timeout
@pytest.mark.skip
async def test_timeout_query(conn_cnx):
    async with conn_cnx() as cnx:
        async with cnx.cursor() as c:
            with pytest.raises(errors.ProgrammingError) as err:
                await c.execute(
                    "select seq8() as c1 from table(generator(timeLimit => 60))",
                    timeout=5,
                )
            assert err.value.errno == 604, "Invalid error code"


async def test_executemany(conn, db_parameters):
    """Executes many statements. Client binding is supported by either dict, or list data types.

    Notes:
        The binding data type is dict and tuple, respectively.
    """
    table_name = random_string(5, "test_executemany_")
    async with conn() as cnx:
        async with cnx.cursor() as c:
            await c.execute(f"create temp table {table_name} (aa number)")
            await c.executemany(
                f"insert into {table_name}(aa) values(%(value)s)",
                [
                    {"value": 1234},
                    {"value": 234},
                    {"value": 34},
                    {"value": 4},
                ],
            )
            assert (await c.fetchone())[0] == 4, "number of records"
            assert c.rowcount == 4, "wrong number of records were inserted"

        async with cnx.cursor() as c:
            fmt = "insert into {name}(aa) values(%s)".format(name=db_parameters["name"])
            await c.executemany(
                fmt,
                [
                    (12345,),
                    (1234,),
                    (234,),
                    (34,),
                    (4,),
                ],
            )
            assert (await c.fetchone())[0] == 5, "number of records"
            assert c.rowcount == 5, "wrong number of records were inserted"


async def test_executemany_qmark_types(conn, db_parameters):
    table_name = random_string(5, "test_executemany_qmark_types_")
    async with conn(paramstyle="qmark") as cnx:
        async with cnx.cursor() as cur:
            await cur.execute(f"create temp table {table_name} (birth_date date)")

            insert_qy = f"INSERT INTO {table_name} (birth_date) values (?)"
            date_1, date_2, date_3, date_4 = (
                date(1969, 2, 7),
                date(1969, 1, 1),
                date(2999, 12, 31),
                date(9999, 1, 1),
            )

            # insert two dates, one in tuple format which specifies
            # the snowflake type similar to how we support it in this
            # example:
            # https://docs.snowflake.com/en/user-guide/python-connector-example.html#using-qmark-or-numeric-binding-with-datetime-objects
            await cur.executemany(
                insert_qy,
                [[date_1], [("DATE", date_2)], [date_3], [date_4]],
                # test that kwargs get passed through executemany properly
                _statement_params={
                    PARAMETER_PYTHON_CONNECTOR_QUERY_RESULT_FORMAT: "json"
                },
            )
            assert all(
                isinstance(rb, JSONResultBatch) for rb in await cur.get_result_batches()
            )

            await cur.execute(f"select * from {table_name}")
            assert {row[0] async for row in cur} == {date_1, date_2, date_3, date_4}


async def test_executemany_params_iterator(conn):
    """Cursor.executemany() works with an interator of params."""
    table_name = random_string(5, "executemany_params_iterator_")
    async with conn() as cnx:
        async with cnx.cursor() as c:
            await c.execute(f"create temp table {table_name}(bar integer)")
            fmt = f"insert into {table_name}(bar) values(%(value)s)"
            await c.executemany(fmt, ({"value": x} for x in ("1234", "234", "34", "4")))
            assert (await c.fetchone())[0] == 4, "number of records"
            assert c.rowcount == 4, "wrong number of records were inserted"

        async with cnx.cursor() as c:
            fmt = f"insert into {table_name}(bar) values(%s)"
            await c.executemany(fmt, ((x,) for x in (12345, 1234, 234, 34, 4)))
            assert (await c.fetchone())[0] == 5, "number of records"
            assert c.rowcount == 5, "wrong number of records were inserted"


async def test_executemany_empty_params(conn):
    """Cursor.executemany() does nothing if params is empty."""
    table_name = random_string(5, "executemany_empty_params_")
    async with conn() as cnx:
        async with cnx.cursor() as c:
            # The table isn't created, so if this were executed, it would error.
            await c.executemany(f"insert into {table_name}(aa) values(%(value)s)", [])
            assert c.query is None


async def test_closed_cursor(conn, db_parameters):
    """Attempts to use the closed cursor. It should raise errors.

    Notes:
        The binding data type is scalar.
    """
    table_name = random_string(5, "test_closed_cursor_")
    async with conn() as cnx:
        async with cnx.cursor() as c:
            await c.execute(f"create temp table {table_name} (aa number)")
            fmt = f"insert into {table_name}(aa) values(%s)"
            await c.executemany(
                fmt,
                [
                    12345,
                    1234,
                    234,
                    34,
                    4,
                ],
            )
            assert (await c.fetchone())[0] == 5, "number of records"
            assert c.rowcount == 5, "number of records"

        with pytest.raises(InterfaceError, match="Cursor is closed in execute") as err:
            await c.execute(f"select aa from {table_name}")
        assert err.value.errno == errorcode.ER_CURSOR_IS_CLOSED
        assert (
            c.rowcount == 5
        ), "SNOW-647539: rowcount should remain available after cursor is closed"


async def test_fetchmany(conn, db_parameters, caplog):
    table_name = random_string(5, "test_fetchmany_")
    async with conn() as cnx:
        async with cnx.cursor() as c:
            await c.execute(f"create temp table {table_name} (aa number)")
            await c.executemany(
                f"insert into {table_name}(aa) values(%(value)s)",
                [
                    {"value": "3456789"},
                    {"value": "234567"},
                    {"value": "1234"},
                    {"value": "234"},
                    {"value": "34"},
                    {"value": "4"},
                ],
            )
            assert (await c.fetchone())[0] == 6, "number of records"
            assert c.rowcount == 6, "number of records"

        async with cnx.cursor() as c:
            await c.execute(f"select aa from {table_name} order by aa desc")
            assert "Number of results in first chunk: 6" in caplog.text

            rows = await c.fetchmany(2)
            assert len(rows) == 2, "The number of records"
            assert rows[1][0] == 234567, "The second record"

            rows = await c.fetchmany(1)
            assert len(rows) == 1, "The number of records"
            assert rows[0][0] == 1234, "The first record"

            rows = await c.fetchmany(5)
            assert len(rows) == 3, "The number of records"
            assert rows[-1][0] == 4, "The last record"

            assert len(await c.fetchmany(15)) == 0, "The number of records"


async def test_process_params(conn, db_parameters):
    """Binds variables for insert and other queries."""
    table_name = random_string(5, "test_process_params_")
    async with conn() as cnx:
        async with cnx.cursor() as c:
            await c.execute(f"create temp table {table_name} (aa number)")
            await c.executemany(
                f"insert into {table_name}(aa) values(%(value)s)",
                [
                    {"value": "3456789"},
                    {"value": "234567"},
                    {"value": "1234"},
                    {"value": "234"},
                    {"value": "34"},
                    {"value": "4"},
                ],
            )
            assert (await c.fetchone())[0] == 6, "number of records"

        async with cnx.cursor() as c:
            await c.execute(
                f"select count(aa) from {table_name} where aa > %(value)s",
                {"value": 1233},
            )
            assert (await c.fetchone())[0] == 3, "the number of records"

        async with cnx.cursor() as c:
            await c.execute(
                f"select count(aa) from {table_name} where aa > %s", (1234,)
            )
            assert (await c.fetchone())[0] == 2, "the number of records"


@pytest.mark.parametrize(
    ("interpolate_empty_sequences", "expected_outcome"), [(False, "%%s"), (True, "%s")]
)
async def test_process_params_empty(
    conn_cnx, interpolate_empty_sequences, expected_outcome
):
    """SQL is interpolated if params aren't None."""
    async with conn_cnx(interpolate_empty_sequences=interpolate_empty_sequences) as cnx:
        async with cnx.cursor() as cursor:
            await cursor.execute("select '%%s'", None)
            assert await cursor.fetchone() == ("%%s",)
            await cursor.execute("select '%%s'", ())
            assert await cursor.fetchone() == (expected_outcome,)


async def test_real_decimal(conn, db_parameters):
    async with conn() as cnx:
        c = cnx.cursor()
        fmt = ("insert into {name}(aa, pct, ratio) " "values(%s,%s,%s)").format(
            name=db_parameters["name"]
        )
        await c.execute(fmt, (9876, 12.3, decimal.Decimal("23.4")))
        async for (_cnt,) in c:
            pass
        assert _cnt == 1, "the number of records"
        await c.close()

        c = cnx.cursor()
        fmt = "select aa, pct, ratio from {name}".format(name=db_parameters["name"])
        await c.execute(fmt)
        async for _aa, _pct, _ratio in c:
            pass
        assert _aa == 9876, "the integer value"
        assert _pct == 12.3, "the float value"
        assert _ratio == decimal.Decimal("23.4"), "the decimal value"
        await c.close()

        async with cnx.cursor(snowflake.connector.aio.DictCursor) as c:
            fmt = "select aa, pct, ratio from {name}".format(name=db_parameters["name"])
            await c.execute(fmt)
            rec = await c.fetchone()
            assert rec["AA"] == 9876, "the integer value"
            assert rec["PCT"] == 12.3, "the float value"
            assert rec["RATIO"] == decimal.Decimal("23.4"), "the decimal value"


async def test_none_errorhandler(conn_testaccount):
    c = conn_testaccount.cursor()
    with pytest.raises(errors.ProgrammingError):
        c.errorhandler = None


async def test_nope_errorhandler(conn_testaccount):
    def user_errorhandler(connection, cursor, errorclass, errorvalue):
        pass

    c = conn_testaccount.cursor()
    c.errorhandler = user_errorhandler
    await c.execute("select * foooooo never_exists_table")
    await c.execute("select * barrrrr never_exists_table")
    await c.execute("select * daaaaaa never_exists_table")
    assert c.messages[0][0] == errors.ProgrammingError, "One error was recorded"
    assert len(c.messages) == 1, "should be one error"


@pytest.mark.internal
async def test_binding_negative(negative_conn_cnx, db_parameters):
    async with negative_conn_cnx() as cnx:
        with pytest.raises(TypeError):
            await cnx.cursor().execute(
                "INSERT INTO {name}(aa) VALUES(%s)".format(name=db_parameters["name"]),
                (1, 2, 3),
            )
        with pytest.raises(errors.ProgrammingError):
            await cnx.cursor().execute(
                "INSERT INTO {name}(aa) VALUES(%s)".format(name=db_parameters["name"]),
                (),
            )
        with pytest.raises(errors.ProgrammingError):
            await cnx.cursor().execute(
                "INSERT INTO {name}(aa) VALUES(%s)".format(name=db_parameters["name"]),
                (["a"],),
            )


async def test_execute_stores_query(conn_cnx):
    async with conn_cnx() as cnx:
        async with cnx.cursor() as cursor:
            assert cursor.query is None
            await cursor.execute("select 1")
            assert cursor.query == "select 1"


async def test_execute_after_close(conn_testaccount):
    """SNOW-13588: Raises an error if executing after the connection is closed."""
    cursor = conn_testaccount.cursor()
    await conn_testaccount.close()
    with pytest.raises(errors.Error):
        await cursor.execute("show tables")


async def test_multi_table_insert(conn, db_parameters):
    try:
        async with conn() as cnx:
            cur = cnx.cursor()
            await cur.execute(
                """
    INSERT INTO {name}(aa) VALUES(1234),(9876),(2345)
    """.format(
                    name=db_parameters["name"]
                )
            )
            assert cur.rowcount == 3, "the number of records"

            await cur.execute(
                """
CREATE OR REPLACE TABLE {name}_foo (aa_foo int)
    """.format(
                    name=db_parameters["name"]
                )
            )

            await cur.execute(
                """
CREATE OR REPLACE TABLE {name}_bar (aa_bar int)
    """.format(
                    name=db_parameters["name"]
                )
            )

            await cur.execute(
                """
INSERT ALL
    INTO {name}_foo(aa_foo) VALUES(aa)
    INTO {name}_bar(aa_bar) VALUES(aa)
    SELECT aa FROM {name}
    """.format(
                    name=db_parameters["name"]
                )
            )
            assert cur.rowcount == 6
    finally:
        async with conn() as cnx:
            await cnx.cursor().execute(
                """
DROP TABLE IF EXISTS {name}_foo
""".format(
                    name=db_parameters["name"]
                )
            )
            await cnx.cursor().execute(
                """
DROP TABLE IF EXISTS {name}_bar
""".format(
                    name=db_parameters["name"]
                )
            )


@pytest.mark.skipif(
    True,
    reason="""
Negative test case.
""",
)
async def test_fetch_before_execute(conn_testaccount):
    """SNOW-13574: Fetch before execute."""
    cursor = conn_testaccount.cursor()
    with pytest.raises(errors.DataError):
        await cursor.fetchone()


async def test_close_twice(conn_testaccount):
    await conn_testaccount.close()
    await conn_testaccount.close()


@pytest.mark.parametrize("result_format", ("arrow", "json"))
async def test_fetch_out_of_range_timestamp_value(conn, result_format):
    async with conn() as cnx:
        cur = cnx.cursor()
        await cur.execute(
            f"alter session set python_connector_query_result_format='{result_format}'"
        )
        await cur.execute("select '12345-01-02'::timestamp_ntz")
        with pytest.raises(errors.InterfaceError):
            await cur.fetchone()


async def test_null_in_non_null(conn):
    table_name = random_string(5, "null_in_non_null")
    error_msg = "NULL result in a non-nullable column"
    async with conn() as cnx:
        cur = cnx.cursor()
        await cur.execute(f"create temp table {table_name}(bar char not null)")
        with pytest.raises(errors.IntegrityError, match=error_msg):
            await cur.execute(f"insert into {table_name} values (null)")


@pytest.mark.parametrize("sql", (None, ""), ids=["None", "empty"])
async def test_empty_execution(conn, sql):
    """Checks whether executing an empty string, or nothing behaves as expected."""
    async with conn() as cnx:
        async with cnx.cursor() as cur:
            if sql is not None:
                await cur.execute(sql)
            assert cur._result is None
            with pytest.raises(
                TypeError, match="'NoneType' object is not( an)? itera(tor|ble)"
            ):
                await cur.fetchone()
            with pytest.raises(
                TypeError, match="'NoneType' object is not( an)? itera(tor|ble)"
            ):
                await cur.fetchall()


@pytest.mark.parametrize("reuse_results", [False, True])
async def test_reset_fetch(conn, reuse_results):
    """Tests behavior after resetting an open cursor."""
    async with conn(reuse_results=reuse_results) as cnx:
        async with cnx.cursor() as cur:
            await cur.execute("select 1")
            assert cur.rowcount == 1
            cur.reset()
            assert (
                cur.rowcount is None
            ), "calling reset on an open cursor should unset rowcount"
            assert not cur.is_closed(), "calling reset should not close the cursor"
            if reuse_results:
                assert await cur.fetchone() == (1,)
            else:
                assert await cur.fetchone() is None
                assert len(await cur.fetchall()) == 0


async def test_rownumber(conn):
    """Checks whether rownumber is returned as expected."""
    async with conn() as cnx:
        async with cnx.cursor() as cur:
            assert await cur.execute("select * from values (1), (2)")
            assert cur.rownumber is None
            assert await cur.fetchone() == (1,)
            assert cur.rownumber == 0
            assert await cur.fetchone() == (2,)
            assert cur.rownumber == 1


async def test_values_set(conn):
    """Checks whether a bunch of properties start as Nones, but get set to something else when a query was executed."""
    properties = [
        "timestamp_output_format",
        "timestamp_ltz_output_format",
        "timestamp_tz_output_format",
        "timestamp_ntz_output_format",
        "date_output_format",
        "timezone",
        "time_output_format",
        "binary_output_format",
    ]
    async with conn() as cnx:
        async with cnx.cursor() as cur:
            for property in properties:
                assert getattr(cur, property) is None
            # use a statement that alters session parameters due to HTAP optimization
            assert await (
                await cur.execute("alter session set TIMEZONE='America/Los_Angeles'")
            ).fetchone() == ("Statement executed successfully.",)
            # The default values might change in future, so let's just check that they aren't None anymore
            for property in properties:
                assert getattr(cur, property) is not None


async def test_execute_helper_params_error(conn_testaccount):
    """Tests whether calling _execute_helper with a non-dict statement params is handled correctly."""
    async with conn_testaccount.cursor() as cur:
        with pytest.raises(
            ProgrammingError,
            match=r"The data type of statement params is invalid. It must be dict.$",
        ):
            await cur._execute_helper("select %()s", statement_params="1")


async def test_desc_rewrite(conn, caplog):
    """Tests whether describe queries are rewritten as expected and this action is logged."""
    async with conn() as cnx:
        async with cnx.cursor() as cur:
            table_name = random_string(5, "test_desc_rewrite_")
            try:
                await cur.execute(f"create or replace table {table_name} (a int)")
                caplog.set_level(logging.DEBUG, "snowflake.connector")
                await cur.execute(f"desc {table_name}")
                assert (
                    "snowflake.connector.aio._cursor",
                    10,
                    "query was rewritten: org=desc {table_name}, new=describe table {table_name}".format(
                        table_name=table_name
                    ),
                ) in caplog.record_tuples
            finally:
                await cur.execute(f"drop table {table_name}")


@pytest.mark.parametrize("result_format", [False, None, "json"])
async def test_execute_helper_cannot_use_arrow(conn_cnx, caplog, result_format):
    """Tests whether cannot use arrow is handled correctly inside of _execute_helper."""
    async with conn_cnx() as cnx:
        async with cnx.cursor() as cur:
            with mock.patch(
                "snowflake.connector.cursor.CAN_USE_ARROW_RESULT_FORMAT", False
            ):
                if result_format is False:
                    result_format = None
                else:
                    result_format = {
                        PARAMETER_PYTHON_CONNECTOR_QUERY_RESULT_FORMAT: result_format
                    }
                caplog.set_level(logging.DEBUG, "snowflake.connector")
                await cur.execute("select 1", _statement_params=result_format)
                assert (
                    "snowflake.connector.aio._cursor",
                    logging.DEBUG,
                    "Cannot use arrow result format, fallback to json format",
                ) in caplog.record_tuples
                assert await cur.fetchone() == (1,)


async def test_execute_helper_cannot_use_arrow_exception(conn_cnx):
    """Like test_execute_helper_cannot_use_arrow but when we are trying to force arrow an Exception should be raised."""
    async with conn_cnx() as cnx:
        async with cnx.cursor() as cur:
            with mock.patch(
                "snowflake.connector.cursor.CAN_USE_ARROW_RESULT_FORMAT", False
            ):
                with pytest.raises(
                    ProgrammingError,
                    match="The result set in Apache Arrow format is not supported for the platform.",
                ):
                    await cur.execute(
                        "select 1",
                        _statement_params={
                            PARAMETER_PYTHON_CONNECTOR_QUERY_RESULT_FORMAT: "arrow"
                        },
                    )


async def test_check_can_use_arrow_resultset(conn_cnx, caplog):
    """Tests check_can_use_arrow_resultset has no effect when we can use arrow."""
    async with conn_cnx() as cnx:
        async with cnx.cursor() as cur:
            with mock.patch(
                "snowflake.connector.cursor.CAN_USE_ARROW_RESULT_FORMAT", True
            ):
                caplog.set_level(logging.DEBUG, "snowflake.connector")
                cur.check_can_use_arrow_resultset()
    assert "Arrow" not in caplog.text


@pytest.mark.parametrize("snowsql", [True, False])
async def test_check_cannot_use_arrow_resultset(conn_cnx, caplog, snowsql):
    """Tests check_can_use_arrow_resultset expected outcomes."""
    config = {}
    if snowsql:
        config["application"] = "SnowSQL"
    async with conn_cnx(**config) as cnx:
        async with cnx.cursor() as cur:
            with mock.patch(
                "snowflake.connector.cursor.CAN_USE_ARROW_RESULT_FORMAT", False
            ):
                with pytest.raises(
                    ProgrammingError,
                    match=(
                        "Currently SnowSQL doesn't support the result set in Apache Arrow format."
                        if snowsql
                        else "The result set in Apache Arrow format is not supported for the platform."
                    ),
                ) as pe:
                    cur.check_can_use_arrow_resultset()
                    assert pe.errno == (
                        ER_NO_PYARROW_SNOWSQL if snowsql else ER_NO_ARROW_RESULT
                    )


async def test_check_can_use_pandas(conn_cnx):
    """Tests check_can_use_arrow_resultset has no effect when we can import pandas."""
    async with conn_cnx() as cnx:
        async with cnx.cursor() as cur:
            with mock.patch("snowflake.connector.cursor.installed_pandas", True):
                cur.check_can_use_pandas()


async def test_check_cannot_use_pandas(conn_cnx):
    """Tests check_can_use_arrow_resultset has expected outcomes."""
    async with conn_cnx() as cnx:
        async with cnx.cursor() as cur:
            with mock.patch("snowflake.connector.cursor.installed_pandas", False):
                with pytest.raises(
                    ProgrammingError,
                    match=r"Optional dependency: 'pandas' is not installed, please see the "
                    "following link for install instructions: https:.*",
                ) as pe:
                    cur.check_can_use_pandas()
                    assert pe.errno == ER_NO_PYARROW


async def test_not_supported_pandas(conn_cnx):
    """Check that fetch_pandas functions return expected error when arrow results are not available."""
    result_format = {PARAMETER_PYTHON_CONNECTOR_QUERY_RESULT_FORMAT: "json"}
    async with conn_cnx() as cnx:
        async with cnx.cursor() as cur:
            await cur.execute("select 1", _statement_params=result_format)
            with mock.patch("snowflake.connector.cursor.installed_pandas", True):
                with pytest.raises(NotSupportedError):
                    await cur.fetch_pandas_all()
                with pytest.raises(NotSupportedError):
                    list(await cur.fetch_pandas_batches())


async def test_query_cancellation(conn_cnx):
    """Tests whether query_cancellation works."""
    async with conn_cnx() as cnx:
        async with cnx.cursor() as cur:
            await cur.execute(
                "select max(seq8()) from table(generator(timeLimit=>30));",
                _no_results=True,
            )
            sf_qid = cur.sfqid
            await cur.abort_query(sf_qid)


async def test_executemany_insert_rewrite(conn_cnx):
    """Tests calling executemany with a non rewritable pyformat insert query."""
    async with conn_cnx() as con:
        async with con.cursor() as cur:
            with pytest.raises(
                InterfaceError, match="Failed to rewrite multi-row insert"
            ) as ie:
                await cur.executemany("insert into numbers (select 1)", [1, 2])
                assert ie.errno == ER_FAILED_TO_REWRITE_MULTI_ROW_INSERT


async def test_executemany_bulk_insert_size_mismatch(conn_cnx):
    """Tests bulk insert error with variable length of arguments."""
    async with conn_cnx(paramstyle="qmark") as con:
        async with con.cursor() as cur:
            with pytest.raises(
                InterfaceError, match="Bulk data size don't match. expected: 1, got: 2"
            ) as ie:
                await cur.executemany("insert into numbers values (?,?)", [[1], [1, 2]])
                assert ie.errno == ER_FAILED_TO_REWRITE_MULTI_ROW_INSERT


async def test_fetchmany_size_error(conn_cnx):
    """Tests retrieving a negative number of results."""
    async with conn_cnx() as con:
        async with con.cursor() as cur:
            await cur.execute("select 1")
            with pytest.raises(
                ProgrammingError,
                match="The number of rows is not zero or positive number: -1",
            ) as ie:
                await cur.fetchmany(-1)
                assert ie.errno == ER_NOT_POSITIVE_SIZE


async def test_scroll(conn_cnx):
    """Tests if scroll returns a NotSupported exception."""
    async with conn_cnx() as con:
        async with con.cursor() as cur:
            with pytest.raises(
                NotSupportedError, match="scroll is not supported."
            ) as nse:
                await cur.scroll(2)
                assert nse.errno == SQLSTATE_FEATURE_NOT_SUPPORTED


@pytest.mark.xfail(reason="SNOW-1572217 async telemetry support")
async def test__log_telemetry_job_data(conn_cnx, caplog):
    """Tests whether we handle missing connection object correctly while logging a telemetry event."""
    async with conn_cnx() as con:
        async with con.cursor() as cur:
            with mock.patch.object(cur, "_connection", None):
                caplog.set_level(logging.DEBUG, "snowflake.connector")
                await cur._log_telemetry_job_data(
                    TelemetryField.ARROW_FETCH_ALL, True
                )  # dummy value
    assert (
        "snowflake.connector.cursor",
        logging.WARNING,
        "Cursor failed to log to telemetry. Connection object may be None.",
    ) in caplog.record_tuples


@pytest.mark.skip(reason="SNOW-1572217 async telemetry support")
@pytest.mark.parametrize(
    "result_format,expected_chunk_type",
    (
        ("json", JSONResultBatch),
        ("arrow", ArrowResultBatch),
    ),
)
async def test_resultbatch(
    conn_cnx,
    result_format,
    expected_chunk_type,
    capture_sf_telemetry,
):
    """This test checks the following things:
    1. After executing a query can we pickle the result batches
    2. When we get the batches, do we emit a telemetry log
    3. Whether we can iterate through ResultBatches multiple times
    4. Whether the results make sense
    5. See whether getter functions are working
    """
    rowcount = 100000
    async with conn_cnx(
        session_parameters={
            "python_connector_query_result_format": result_format,
        }
    ) as con:
        with capture_sf_telemetry.patch_connection(con) as telemetry_data:
            with con.cursor() as cur:
                cur.execute(
                    f"select seq4() from table(generator(rowcount => {rowcount}));"
                )
                assert cur._result_set.total_row_index() == rowcount
                pre_pickle_partitions = cur.get_result_batches()
                assert len(pre_pickle_partitions) > 1
                assert pre_pickle_partitions is not None
                assert all(
                    isinstance(p, expected_chunk_type) for p in pre_pickle_partitions
                )
                pickle_str = pickle.dumps(pre_pickle_partitions)
                assert any(
                    t.message["type"] == TelemetryField.GET_PARTITIONS_USED.value
                    for t in telemetry_data.records
                )
    post_pickle_partitions: list[ResultBatch] = pickle.loads(pickle_str)
    total_rows = 0
    # Make sure the batches can be iterated over individually
    for i, partition in enumerate(post_pickle_partitions):
        # Tests whether the getter functions are working
        if i == 0:
            assert partition.compressed_size is None
            assert partition.uncompressed_size is None
        else:
            assert partition.compressed_size is not None
            assert partition.uncompressed_size is not None
        for row in partition:
            col1 = row[0]
            assert col1 == total_rows
            total_rows += 1
    assert total_rows == rowcount
    total_rows = 0
    # Make sure the batches can be iterated over again
    for partition in post_pickle_partitions:
        for row in partition:
            col1 = row[0]
            assert col1 == total_rows
            total_rows += 1
    assert total_rows == rowcount


@pytest.mark.parametrize(
    "result_format,patch_path",
    (
        ("json", "snowflake.connector.aio._result_batch.JSONResultBatch.create_iter"),
        ("arrow", "snowflake.connector.aio._result_batch.ArrowResultBatch.create_iter"),
    ),
)
async def test_resultbatch_lazy_fetching_and_schemas(
    conn_cnx, result_format, patch_path
):
    """Tests whether pre-fetching results chunks fetches the right amount of them."""
    rowcount = 1000000  # We need at least 5 chunks for this test
    async with conn_cnx(
        session_parameters={
            "python_connector_query_result_format": result_format,
        }
    ) as con:
        async with con.cursor() as cur:
            # Dummy return value necessary to not iterate through every batch with
            #  first fetchone call

            downloads = [iter([(i,)]) for i in range(10)]

            with mock.patch(
                patch_path,
                side_effect=downloads,
            ) as patched_download:
                await cur.execute(
                    f"select seq4() as c1, randstr(1,random()) as c2 "
                    f"from table(generator(rowcount => {rowcount}));"
                )
                result_batches = await cur.get_result_batches()
                batch_schemas = [batch.schema for batch in result_batches]
                for schema in batch_schemas:
                    # all batches should have the same schema
                    assert schema == [
                        ResultMetadata("C1", 0, None, None, 10, 0, False),
                        ResultMetadata("C2", 2, None, 16777216, None, None, False),
                    ]
                assert patched_download.call_count == 0
                assert len(result_batches) > 5
                assert result_batches[0]._local  # Sanity check first chunk being local
                await cur.fetchone()  # Trigger pre-fetching

                # While the first chunk is local we still call _download on it, which
                # short circuits and just parses (for JSON batches) and then returns
                # an iterator through that data, so we expect the call count to be 5.
                # (0 local and 1, 2, 3, 4 pre-fetched) = 5 total
                start_time = time.time()
                while time.time() < start_time + 1:
                    # TODO: fix me, call count is different
                    if patched_download.call_count == 5:
                        break
                else:
                    assert patched_download.call_count == 5


@pytest.mark.parametrize("result_format", ["json", "arrow"])
async def test_resultbatch_schema_exists_when_zero_rows(conn_cnx, result_format):
    async with conn_cnx(
        session_parameters={"python_connector_query_result_format": result_format}
    ) as con:
        async with con.cursor() as cur:
            await cur.execute(
                "select seq4() as c1, randstr(1,random()) as c2 from table(generator(rowcount => 1)) where 1=0"
            )
            result_batches = await cur.get_result_batches()
            # verify there is 1 batch and 0 rows in that batch
            assert len(result_batches) == 1
            assert result_batches[0].rowcount == 0
            # verify that the schema is correct
            schema = result_batches[0].schema
            assert schema == [
                ResultMetadata("C1", 0, None, None, 10, 0, False),
                ResultMetadata("C2", 2, None, 16777216, None, None, False),
            ]


@pytest.mark.skip("TODO: async telemetry SNOW-1572217")
async def test_optional_telemetry(conn_cnx, capture_sf_telemetry):
    """Make sure that we do not fail when _first_chunk_time is not present in cursor."""
    with conn_cnx() as con:
        with con.cursor() as cur:
            with capture_sf_telemetry.patch_connection(con, False) as telemetry:
                cur.execute("select 1;")
                cur._first_chunk_time = None
                assert cur.fetchall() == [
                    (1,),
                ]
            assert not any(
                r.message.get("type", "")
                == TelemetryField.TIME_CONSUME_LAST_RESULT.value
                for r in telemetry.records
            )


@pytest.mark.parametrize("result_format", ("json", "arrow"))
@pytest.mark.parametrize("cursor_type", (SnowflakeCursor, DictCursor))
@pytest.mark.parametrize("fetch_method", ("__next__", "fetchone"))
async def test_out_of_range_year(conn_cnx, result_format, cursor_type, fetch_method):
    """Tests whether the year 10000 is out of range exception is raised as expected."""
    async with conn_cnx(
        session_parameters={
            PARAMETER_PYTHON_CONNECTOR_QUERY_RESULT_FORMAT: result_format
        }
    ) as con:
        async with con.cursor(cursor_type) as cur:
            await cur.execute(
                "select * from VALUES (1, TO_TIMESTAMP('9999-01-01 00:00:00')), (2, TO_TIMESTAMP('10000-01-01 00:00:00'))"
            )
            iterate_obj = cur if fetch_method == "fetchone" else iter(cur)
            fetch_next_fn = getattr(iterate_obj, fetch_method)
            # first fetch doesn't raise error
            await fetch_next_fn()
            with pytest.raises(
                InterfaceError,
                match=(
                    "date value out of range"
                    if IS_WINDOWS
                    else "year 10000 is out of range"
                ),
            ):
                await fetch_next_fn()


async def test_describe(conn_cnx):
    async with conn_cnx() as con:
        async with con.cursor() as cur:
            for describe in [cur.describe, cur._describe_internal]:
                table_name = random_string(5, "test_describe_")
                # test select
                description = await describe(
                    "select * from VALUES(1, 3.1415926, 'snow', TO_TIMESTAMP('2021-01-01 00:00:00'))"
                )
                assert description is not None
                column_types = [column.type_code for column in description]
                assert constants.FIELD_ID_TO_NAME[column_types[0]] == "FIXED"
                assert constants.FIELD_ID_TO_NAME[column_types[1]] == "FIXED"
                assert constants.FIELD_ID_TO_NAME[column_types[2]] == "TEXT"
                assert "TIMESTAMP" in constants.FIELD_ID_TO_NAME[column_types[3]]
                assert len(await cur.fetchall()) == 0

                # test insert
                await cur.execute(f"create table {table_name} (aa int)")
                try:
                    description = await describe(
                        "insert into {name}(aa) values({value})".format(
                            name=table_name, value="1234"
                        )
                    )
                    assert description[0].name == "number of rows inserted"
                    assert cur.rowcount is None
                finally:
                    await cur.execute(f"drop table if exists {table_name}")


async def test_fetch_batches_with_sessions(conn_cnx):
    rowcount = 250_000
    async with conn_cnx() as con:
        async with con.cursor() as cur:
            await cur.execute(
                f"select seq4() as foo from table(generator(rowcount=>{rowcount}))"
            )

            num_batches = len(await cur.get_result_batches())

            with mock.patch(
                "snowflake.connector.aio._network.SnowflakeRestful._use_requests_session",
                side_effect=con._rest._use_requests_session,
            ) as get_session_mock:
                result = await cur.fetchall()
                # all but one batch is downloaded using a session
                assert get_session_mock.call_count == num_batches - 1
                assert len(result) == rowcount


async def test_null_connection(conn_cnx):
    retries = 15
    async with conn_cnx() as con:
        async with con.cursor() as cur:
            await cur.execute_async(
                "select seq4() as c from table(generator(rowcount=>50000))"
            )
            await con.rest.delete_session()
            status = await con.get_query_status(cur.sfqid)
            for _ in range(retries):
                if status not in (QueryStatus.RUNNING,):
                    break
                await asyncio.sleep(1)
                status = await con.get_query_status(cur.sfqid)
            else:
                pytest.fail(f"query is still running after {retries} retries")
            assert status == QueryStatus.FAILED_WITH_ERROR
            assert con.is_an_error(status)


async def test_multi_statement_failure(conn_cnx):
    """
    This test mocks the driver version sent to Snowflake to be 2.8.1, which does not support multi-statement.
    The backend should not allow multi-statements to be submitted for versions older than 2.9.0 and should raise an
    error when a multi-statement is submitted, regardless of the MULTI_STATEMENT_COUNT parameter.
    """
    try:
        connection.DEFAULT_CONFIGURATION["internal_application_version"] = (
            "2.8.1",
            (type(None), str),
        )
        async with conn_cnx() as con:
            async with con.cursor() as cur:
                with pytest.raises(
                    ProgrammingError,
                    match="Multiple SQL statements in a single API call are not supported; use one API call per statement instead.",
                ):
                    await cur.execute(
                        f"alter session set {PARAMETER_MULTI_STATEMENT_COUNT}=0"
                    )
                    await cur.execute("select 1; select 2; select 3;")
    finally:
        connection.DEFAULT_CONFIGURATION["internal_application_version"] = (
            CLIENT_VERSION,
            (type(None), str),
        )


async def test_decoding_utf8_for_json_result(conn_cnx):
    # SNOW-787480, if not explicitly setting utf-8 decoding, the data will be
    # detected decoding as windows-1250 by chardet.detect
    async with conn_cnx(
        session_parameters={"python_connector_query_result_format": "JSON"}
    ) as con, con.cursor() as cur:
        sql = """select '"",' || '"",' || '"",' || '"",' || '"",' || 'Ofigràfic' || '"",' from TABLE(GENERATOR(ROWCOUNT => 5000)) v;"""
        ret = await (await cur.execute(sql)).fetchall()
        assert len(ret) == 5000
        # This test case is tricky, for most of the test cases, the decoding is incorrect and can could be different
        # on different platforms, however, due to randomness, in rare cases the decoding is indeed utf-8,
        # the backend behavior is flaky
        assert ret[0] in (
            ('"","","","","",OfigrĂ\xa0fic"",',),  # AWS Cloud
            ('"","","","","",OfigrÃ\xa0fic"",',),  # GCP Mac and Linux Cloud
            ('"","","","","",Ofigr\xc3\\xa0fic"",',),  # GCP Windows Cloud
            (
                '"","","","","",Ofigràfic"",',
            ),  # regression environment gets the correct decoding
        )

    async with conn_cnx(
        session_parameters={"python_connector_query_result_format": "JSON"},
        json_result_force_utf8_decoding=True,
    ) as con, con.cursor() as cur:
        ret = await (await cur.execute(sql)).fetchall()
        assert len(ret) == 5000
        assert ret[0] == ('"","","","","",Ofigràfic"",',)

    result_batch = JSONResultBatch(
        None, None, None, None, None, False, json_result_force_utf8_decoding=True
    )
    mock_resp = mock.Mock()
    mock_resp.content = "À".encode("latin1")
    with pytest.raises(Error):
        await result_batch._load(mock_resp)
