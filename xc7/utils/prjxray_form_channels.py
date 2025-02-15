#!/usr/bin/env python3
""" Classify 7-series nodes and generate channels for required nodes.

Rough structure:

Create initial database import by importing tile types, tile wires, tile pips,
site types and site pins.  After importing tile types, imports the grid of
tiles.  This uses tilegrid.json, tile_type_*.json and site_type_*.json.

Once all tiles are imported, all wires in the grid are added and nodes are
formed from the sea of wires based on the tile connections description
(tileconn.json).

In order to determine what each node is used for, site pins and pips are counted
on each node (count_sites_and_pips_on_nodes).  Depending on the site pin and
pip count, the nodes are classified into one of 4 buckets:

    NULL - An unconnected node
    CHANNEL - A routing node
    EDGE_WITH_MUX - An edge between an IPIN and OPIN.
    EDGES_TO_CHANNEL - An edge between an IPIN/OPIN and a CHANNEL.

Then all CHANNEL are grouped into tracks (form_tracks) and graph nodes are
created for the CHANNELs.  Graph edges are added to connect graph nodes that are
part of the same track.

Note that IPIN and OPIN graph nodes are not added yet, as pins have not been
assigned sides of the VPR tiles yet.  This occurs in
prjxray_assign_tile_pin_direction.


"""

import argparse
import prjxray.db
import prjxray.tile
from prjxray.timing import PvtCorner
import progressbar
import tile_splitter.grid
from lib.rr_graph import points
from lib.rr_graph import tracks
from lib.rr_graph import graph2
import datetime
import os
import os.path
from lib.connection_database import NodeClassification, create_tables

from prjxray_db_cache import DatabaseCache

SINGLE_PRECISION_FLOAT_MIN = 2**-126


def import_site_type(db, write_cur, site_types, site_type_name):
    assert site_type_name not in site_types
    site_type = db.get_site_type(site_type_name)

    if site_type_name in site_types:
        return

    write_cur.execute(
        "INSERT INTO site_type(name) VALUES (?)", (site_type_name, )
    )
    site_types[site_type_name] = write_cur.lastrowid

    for site_pin in site_type.get_site_pins():
        pin_info = site_type.get_site_pin(site_pin)

        write_cur.execute(
            """
INSERT INTO site_pin(name, site_type_pkey, direction)
VALUES
  (?, ?, ?)""", (
                pin_info.name, site_types[site_type_name],
                pin_info.direction.value
            )
        )


def create_get_switch(conn):
    """ Returns functions to get or create switches with various timing.

    Every switch that requires different timing is given it's own switch
    in VPR.  get_switch returns a switch a pip.  get_switch_timing returns
    a switch given a particular timing.

    """
    write_cur = conn.cursor()

    pip_cache = {}

    write_cur.execute(
        "SELECT pkey FROM switch WHERE name = ?",
        ("__vpr_delayless_switch__", )
    )
    pip_cache[(False, 0.0, 0.0, 0.0)] = write_cur.fetchone()[0]

    def get_switch_timing(
            is_pass_transistor, delay, internal_capacitance, drive_resistance
    ):
        """ Return a switch that matches provided timing.

        Arguments
        ---------
        is_pass_transistor : bool-like
            If true, this switch should be represented as a "pass_gate".

        delay : float or convertable to float
            Intrinsic delay through switch (seconds)

        internal_capacitance : float or convertable to float
            Internal capacitance to switch (Farads).

        drive_resistance : float or convertable to float
            Drive resistance from switch (Ohms).

        Returns
        -------
        switch_pkey : int
            Switch primary key that represents provided arguments.

        """
        key = (
            bool(is_pass_transistor), float(delay), float(drive_resistance),
            float(internal_capacitance)
        )

        if key not in pip_cache:
            name = 'routing'
            switch_type = 'mux'
            if is_pass_transistor:
                name = 'pass_transistor'
                switch_type = 'pass_gate'

            name = '{}_R{}_C{}_Tdel{}'.format(
                name, drive_resistance, internal_capacitance, delay
            )

            write_cur.execute(
                """
INSERT INTO switch(name, internal_capacitance, drive_resistance, intrinsic_delay, switch_type)
VALUES
    (?, ?, ?, ?, ?)""", (
                    name, internal_capacitance, drive_resistance, delay,
                    switch_type
                )
            )
            pip_cache[key] = write_cur.lastrowid

            write_cur.connection.commit()

        return pip_cache[key]

    def get_switch(pip, pip_timing):
        """ Return switch_pkey for given pip timing.

        Arguments
        ---------
        pip : tile.Pip object
            Pip being modelled
        pip_timing : tile.PipTiming object
            Pip timing being modelled

        Returns
        -------
        switch_pkey : int
            Switch primary key that represents provided arguments.

        """
        delay = 0.0
        drive_resistance = 0.0
        internal_capacitance = 0.0

        if pip_timing is not None:
            if pip_timing.delays is not None:
                # Use the largest intristic delay for now.
                # This conservative on slack timing, but not on hold timing.
                #
                # nanosecond -> seconds
                delay = pip_timing.delays[PvtCorner.SLOW].max / 1e9

            if pip_timing.internal_capacitance is not None:
                # microFarads -> Farads
                internal_capacitance = pip_timing.internal_capacitance / 1e6

            if pip_timing.drive_resistance is not None:
                # milliOhms -> Ohms
                drive_resistance = pip_timing.drive_resistance / 1e3

        return get_switch_timing(
            pip.is_pass_transistor, delay, internal_capacitance,
            drive_resistance
        )

    return get_switch, get_switch_timing


def import_tile_type(
        db, write_cur, tile_types, site_types, tile_type_name, get_switch
):
    assert tile_type_name not in tile_types
    tile_type = db.get_tile_type(tile_type_name)

    write_cur.execute(
        "INSERT INTO tile_type(name) VALUES (?)", (tile_type_name, )
    )
    tile_types[tile_type_name] = write_cur.lastrowid

    wires = {}
    for wire, wire_rc_element in tile_type.get_wires().items():
        capacitance = 0.0
        resistance = 0.0

        if wire_rc_element is not None:
            # microFarads -> Farads
            capacitance = wire_rc_element.capacitance / 1e6

            # milliOhms -> Ohms
            resistance = wire_rc_element.resistance / 1e3

        write_cur.execute(
            """
INSERT INTO wire_in_tile(name, tile_type_pkey, capacitance, resistance)
VALUES
  (?, ?, ?, ?)""", (wire, tile_types[tile_type_name], capacitance, resistance)
        )
        wires[wire] = write_cur.lastrowid

    for pip in tile_type.get_pips():
        switch_pkey = get_switch(pip, pip.timing)
        backward_switch_pkey = get_switch(pip, pip.backward_timing)

        write_cur.execute(
            """
INSERT INTO pip_in_tile(
  name, tile_type_pkey, src_wire_in_tile_pkey,
  dest_wire_in_tile_pkey, can_invert, is_directional, is_pseudo,
  is_pass_transistor, switch_pkey, backward_switch_pkey
)
VALUES
  (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                pip.name, tile_types[tile_type_name], wires[pip.net_from],
                wires[pip.net_to
                      ], pip.can_invert, pip.is_directional, pip.is_pseudo,
                pip.is_pass_transistor, switch_pkey, backward_switch_pkey
            )
        )

    for site in tile_type.get_sites():
        if site.type not in site_types:
            import_site_type(db, write_cur, site_types, site.type)


def add_wire_to_site_relation(
        db, write_cur, tile_types, site_types, tile_type_name,
        get_switch_timing
):
    tile_type = db.get_tile_type(tile_type_name)
    for site in tile_type.get_sites():
        write_cur.execute(
            """
INSERT INTO site(name, x_coord, y_coord, site_type_pkey)
VALUES
  (?, ?, ?, ?)""", (site.name, site.x, site.y, site_types[site.type])
        )

        site_pkey = write_cur.lastrowid

        for site_pin in site.site_pins:
            write_cur.execute(
                """
SELECT
  pkey
FROM
  site_pin
WHERE
  name = ?
  AND site_type_pkey = ?""", (site_pin.name, site_types[site.type])
            )
            result = write_cur.fetchone()
            site_pin_pkey = result[0]

            intrinsic_delay = 0
            drive_resistance = 0
            capacitance = 0

            if site_pin.timing is not None:
                # Use the largest intristic delay for now.
                # This conservative on slack timing, but not on hold timing.
                #
                # nanosecond -> seconds
                intrinsic_delay = site_pin.timing.delays[PvtCorner.SLOW
                                                         ].max / 1e9

                if isinstance(site_pin.timing, prjxray.tile.OutpinTiming):
                    # milliOhms -> Ohms
                    drive_resistance = site_pin.timing.drive_resistance / 1e3
                elif isinstance(site_pin.timing, prjxray.tile.InpinTiming):
                    # microFarads -> Farads
                    capacitance = site_pin.timing.capacitance / 1e6
                else:
                    assert False, site_pin
            else:
                # Use min value instead of 0 to prevent
                # VPR from freaking out over a zero net delay.
                #
                # Note this is the single precision float minimum, because VPR
                # uses single precision, not double precision.
                intrinsic_delay = SINGLE_PRECISION_FLOAT_MIN

            site_pin_switch_pkey = get_switch_timing(
                is_pass_transistor=False,
                delay=intrinsic_delay,
                internal_capacitance=capacitance,
                drive_resistance=drive_resistance,
            )

            write_cur.execute(
                """
UPDATE
  wire_in_tile
SET
  site_pkey = ?,
  site_pin_pkey = ?,
  site_pin_switch_pkey = ?
WHERE
  name = ?
  and tile_type_pkey = ?;""", (
                    site_pkey, site_pin_pkey, site_pin_switch_pkey,
                    site_pin.wire, tile_types[tile_type_name]
                )
            )


def build_tile_type_indicies(write_cur):
    write_cur.execute(
        "CREATE INDEX site_pin_index ON site_pin(name, site_type_pkey);"
    )
    write_cur.execute(
        "CREATE INDEX wire_name_index ON wire_in_tile(name, tile_type_pkey);"
    )
    write_cur.execute(
        "CREATE INDEX wire_site_pin_index ON wire_in_tile(site_pin_pkey);"
    )
    write_cur.execute(
        "CREATE INDEX tile_type_index ON phy_tile(tile_type_pkey);"
    )
    write_cur.execute(
        "CREATE INDEX pip_tile_type_index ON pip_in_tile(tile_type_pkey);"
    )
    write_cur.execute(
        "CREATE INDEX src_pip_index ON pip_in_tile(src_wire_in_tile_pkey);"
    )
    write_cur.execute(
        "CREATE INDEX dest_pip_index ON pip_in_tile(dest_wire_in_tile_pkey);"
    )


def build_other_indicies(write_cur):
    write_cur.execute("CREATE INDEX phy_tile_name_index ON phy_tile(name);")
    write_cur.execute(
        "CREATE INDEX phy_tile_location_index ON phy_tile(grid_x, grid_y);"
    )


def import_phy_grid(db, grid, conn, get_switch, get_switch_timing):
    write_cur = conn.cursor()

    tile_types = {}
    site_types = {}

    for tile in grid.tiles():
        gridinfo = grid.gridinfo_at_tilename(tile)

        if gridinfo.tile_type not in tile_types:
            if gridinfo.tile_type in tile_types:
                continue

            import_tile_type(
                db, write_cur, tile_types, site_types, gridinfo.tile_type,
                get_switch
            )

    write_cur.connection.commit()

    build_tile_type_indicies(write_cur)
    write_cur.connection.commit()

    for tile_type in tile_types:
        add_wire_to_site_relation(
            db, write_cur, tile_types, site_types, tile_type, get_switch_timing
        )

    for tile in grid.tiles():
        gridinfo = grid.gridinfo_at_tilename(tile)
        loc = grid.loc_of_tilename(tile)
        # tile: pkey name tile_type_pkey grid_x grid_y
        write_cur.execute(
            """
INSERT INTO phy_tile(name, tile_type_pkey, grid_x, grid_y)
VALUES
  (?, ?, ?, ?)""",
            (tile, tile_types[gridinfo.tile_type], loc.grid_x, loc.grid_y)
        )

    build_other_indicies(write_cur)
    write_cur.connection.commit()


def import_nodes(db, grid, conn):
    # Some nodes are just 1 wire, so start by enumerating all wires.

    cur = conn.cursor()
    write_cur = conn.cursor()
    write_cur.execute("""BEGIN EXCLUSIVE TRANSACTION;""")

    tile_wire_map = {}
    wires = {}
    for tile in progressbar.progressbar(grid.tiles()):
        gridinfo = grid.gridinfo_at_tilename(tile)
        tile_type = db.get_tile_type(gridinfo.tile_type)

        cur.execute(
            """SELECT pkey, tile_type_pkey FROM phy_tile WHERE name = ?;""",
            (tile, )
        )
        phy_tile_pkey, tile_type_pkey = cur.fetchone()

        for wire in tile_type.get_wires():
            # pkey node_pkey tile_pkey wire_in_tile_pkey
            cur.execute(
                """
SELECT pkey FROM wire_in_tile WHERE name = ? and tile_type_pkey = ?;""",
                (wire, tile_type_pkey)
            )
            (wire_in_tile_pkey, ) = cur.fetchone()

            write_cur.execute(
                """
INSERT INTO wire(phy_tile_pkey, wire_in_tile_pkey)
VALUES
  (?, ?);""", (phy_tile_pkey, wire_in_tile_pkey)
            )

            assert (tile, wire) not in tile_wire_map
            wire_pkey = write_cur.lastrowid
            tile_wire_map[(tile, wire)] = wire_pkey
            wires[wire_pkey] = None

    write_cur.execute("""COMMIT TRANSACTION;""")

    connections = db.connections()

    for connection in progressbar.progressbar(connections.get_connections()):
        a_pkey = tile_wire_map[
            (connection.wire_a.tile, connection.wire_a.wire)]
        b_pkey = tile_wire_map[
            (connection.wire_b.tile, connection.wire_b.wire)]

        a_node = wires[a_pkey]
        b_node = wires[b_pkey]

        if a_node is None:
            a_node = set((a_pkey, ))

        if b_node is None:
            b_node = set((b_pkey, ))

        if a_node is not b_node:
            a_node |= b_node

            for wire in a_node:
                wires[wire] = a_node

    nodes = {}
    for wire_pkey, node in wires.items():
        if node is None:
            node = set((wire_pkey, ))

        assert wire_pkey in node

        nodes[id(node)] = node

    wires_assigned = set()
    for node in progressbar.progressbar(nodes.values()):
        write_cur.execute("""INSERT INTO node(number_pips) VALUES (0);""")
        node_pkey = write_cur.lastrowid

        for wire_pkey in node:
            wires_assigned.add(wire_pkey)
            write_cur.execute(
                """
            UPDATE wire
                SET node_pkey = ?
                WHERE pkey = ?
            ;""", (node_pkey, wire_pkey)
            )

    assert len(set(wires.keys()) ^ wires_assigned) == 0

    del tile_wire_map
    del nodes
    del wires

    write_cur.execute(
        "CREATE INDEX wire_in_tile_index ON wire(wire_in_tile_pkey);"
    )
    write_cur.execute(
        "CREATE INDEX wire_index ON wire(phy_tile_pkey, wire_in_tile_pkey);"
    )
    write_cur.execute("CREATE INDEX wire_node_index ON wire(node_pkey);")

    write_cur.connection.commit()


def count_sites_and_pips_on_nodes(conn):
    cur = conn.cursor()

    print("{}: Counting sites on nodes".format(datetime.datetime.now()))
    cur.execute(
        """
WITH node_sites(node_pkey, number_site_pins) AS (
  SELECT
    wire.node_pkey,
    count(wire_in_tile.site_pin_pkey)
  FROM
    wire_in_tile
    INNER JOIN wire ON wire.wire_in_tile_pkey = wire_in_tile.pkey
  WHERE
    wire_in_tile.site_pin_pkey IS NOT NULL
  GROUP BY
    wire.node_pkey
)
SELECT
  max(node_sites.number_site_pins)
FROM
  node_sites;
"""
    )

    # Nodes are only expected to have 1 site
    assert cur.fetchone()[0] == 1

    print("{}: Assigning site wires for nodes".format(datetime.datetime.now()))
    cur.execute(
        """
WITH site_wires(wire_pkey, node_pkey) AS (
  SELECT
    wire.pkey,
    wire.node_pkey
  FROM
    wire_in_tile
    INNER JOIN wire ON wire.wire_in_tile_pkey = wire_in_tile.pkey
  WHERE
    wire_in_tile.site_pin_pkey IS NOT NULL
)
UPDATE
  node
SET
  site_wire_pkey = (
    SELECT
      site_wires.wire_pkey
    FROM
      site_wires
    WHERE
      site_wires.node_pkey = node.pkey
  );
      """
    )

    print("{}: Counting pips on nodes".format(datetime.datetime.now()))
    cur.execute(
        """
    CREATE TABLE node_pip_count(
      node_pkey INT,
      number_pips INT,
      FOREIGN KEY(node_pkey) REFERENCES node(pkey)
    );"""
    )
    cur.execute(
        """
INSERT INTO node_pip_count(node_pkey, number_pips)
SELECT
  wire.node_pkey,
  count(pip_in_tile.pkey)
FROM
  pip_in_tile
  INNER JOIN wire
WHERE
  pip_in_tile.is_pseudo = 0 AND (
  pip_in_tile.src_wire_in_tile_pkey = wire.wire_in_tile_pkey
  OR pip_in_tile.dest_wire_in_tile_pkey = wire.wire_in_tile_pkey)
GROUP BY
  wire.node_pkey;"""
    )
    cur.execute("CREATE INDEX pip_count_index ON node_pip_count(node_pkey);")

    print("{}: Inserting pip counts".format(datetime.datetime.now()))
    cur.execute(
        """
UPDATE
  node
SET
  number_pips = (
    SELECT
      node_pip_count.number_pips
    FROM
      node_pip_count
    WHERE
      node_pip_count.node_pkey = node.pkey
  )
WHERE
  pkey IN (
    SELECT
      node_pkey
    FROM
      node_pip_count
  );"""
    )

    cur.execute("""DROP TABLE node_pip_count;""")

    cur.connection.commit()


def check_edge_with_mux_timing(
        conn, get_switch_timing, src_wire_pkey, dest_wire_pkey, pip_pkey
):
    """ Check if edge with mux timing can be "lumped" into the switch.

    Returns
    -------
    switch_pkey : int
        Switch primary key to model EDGE_WITH_MUX connection.

    """

    cur = conn.cursor()

    cur.execute(
        """
SELECT site_pin_switch_pkey, resistance, capacitance FROM wire_in_tile WHERE pkey = (
    SELECT wire_in_tile_pkey FROM wire WHERE pkey = ?
    )""", (src_wire_pkey, )
    )

    src_site_pin_switch_pkey, src_wire_resistance, src_wire_capacitance = cur.fetchone(
    )

    assert src_wire_resistance == 0, src_wire_pkey

    cur.execute(
        """
SELECT intrinsic_delay, drive_resistance FROM switch WHERE pkey = ?
        """, (src_site_pin_switch_pkey, )
    )
    src_site_pin_intrinsic_delay, src_site_pin_drive_resistance = cur.fetchone(
    )

    cur.execute(
        """
SELECT site_pin_switch_pkey, resistance, capacitance FROM wire_in_tile WHERE pkey = (
    SELECT wire_in_tile_pkey FROM wire WHERE pkey = ?
    )""", (dest_wire_pkey, )
    )

    dest_site_pin_switch_pkey, dest_wire_resistance, dest_wire_capacitance = cur.fetchone(
    )

    assert dest_wire_resistance == 0, dest_wire_pkey

    cur.execute(
        """
SELECT intrinsic_delay, internal_capacitance FROM switch WHERE pkey = ?
        """, (dest_site_pin_switch_pkey, )
    )
    dest_site_pin_intrinsic_delay, dest_site_pin_capacitance = cur.fetchone()

    cur.execute(
        """
SELECT name, switch_pkey FROM pip_in_tile WHERE pkey = ?""", (pip_pkey, )
    )
    pip_name, switch_pkey = cur.fetchone()

    cur.execute(
        """
SELECT name, intrinsic_delay, internal_capacitance, drive_resistance, switch_type FROM switch WHERE pkey = ?
        """, (switch_pkey, )
    )
    (
        switch_name, switch_intrinsic_delay, switch_internal_capacitance,
        switch_drive_resistance, switch_type
    ) = cur.fetchone()

    assert switch_type in ["mux", "pass_gate"], (switch_pkey, switch_type)

    zero_delay_to_switch = src_site_pin_drive_resistance == 0 or (
        switch_internal_capacitance == 0 and src_wire_capacitance == 0
    )
    zero_delay_from_switch = switch_drive_resistance == 0 or (
        dest_wire_capacitance == 0 and dest_site_pin_capacitance == 0
    )

    if zero_delay_to_switch and zero_delay_from_switch and \
            src_site_pin_intrinsic_delay == 0 and \
            dest_site_pin_intrinsic_delay == 0:
        return get_switch_timing(
            is_pass_transistor=False,
            delay=switch_intrinsic_delay,
            internal_capacitance=0,
            drive_resistance=0,
        )

    if switch_type == "mux":
        switch_delay = src_site_pin_intrinsic_delay
        switch_delay += src_site_pin_drive_resistance * (
            switch_internal_capacitance + src_wire_capacitance
        )
        switch_delay += switch_intrinsic_delay
        switch_delay += switch_drive_resistance * (
            dest_wire_capacitance + dest_site_pin_capacitance
        )
        switch_delay += dest_site_pin_intrinsic_delay
    else:
        assert switch_type == "pass_gate"
        assert switch_intrinsic_delay == 0
        assert switch_drive_resistance == 0

        switch_delay = src_site_pin_intrinsic_delay
        switch_delay += src_site_pin_drive_resistance * (
            src_wire_capacitance + switch_internal_capacitance +
            dest_wire_capacitance + dest_site_pin_capacitance
        )
        switch_delay += dest_site_pin_intrinsic_delay

    return get_switch_timing(
        is_pass_transistor=False,
        delay=switch_delay,
        internal_capacitance=0,
        drive_resistance=0
    )


def classify_nodes(conn, get_switch_timing):
    write_cur = conn.cursor()

    # Nodes are NULL if they they only have either a site pin or 1 pip, but
    # nothing else.
    write_cur.execute(
        """
UPDATE node SET classification = ?
    WHERE (node.site_wire_pkey IS NULL AND node.number_pips <= 1) OR
          (node.site_wire_pkey IS NOT NULL AND node.number_pips == 0)
    ;""", (NodeClassification.NULL.value, )
    )
    write_cur.execute(
        """
UPDATE node SET classification = ?
    WHERE node.number_pips > 1 and node.site_wire_pkey IS NULL;""",
        (NodeClassification.CHANNEL.value, )
    )
    write_cur.execute(
        """
UPDATE node SET classification = ?
    WHERE node.number_pips > 1 and node.site_wire_pkey IS NOT NULL;""",
        (NodeClassification.EDGES_TO_CHANNEL.value, )
    )

    null_nodes = []
    edges_to_channel = []
    edge_with_mux = []

    cur = conn.cursor()
    cur.execute(
        """
SELECT
  count(pkey)
FROM
  node
WHERE
  number_pips == 1
  AND site_wire_pkey IS NOT NULL;"""
    )
    num_nodes = cur.fetchone()[0]
    with progressbar.ProgressBar(max_value=num_nodes) as bar:
        bar.update(0)
        for idx, (node, site_wire_pkey) in enumerate(cur.execute("""
SELECT
  pkey,
  site_wire_pkey
FROM
  node
WHERE
  number_pips == 1
  AND site_wire_pkey IS NOT NULL;""")):
            bar.update(idx)

            write_cur.execute(
                """
WITH wire_in_node(
  wire_pkey, phy_tile_pkey, wire_in_tile_pkey
) AS (
  SELECT
    wire.pkey,
    wire.phy_tile_pkey,
    wire.wire_in_tile_pkey
  FROM
    wire
  WHERE
    wire.node_pkey = ?
)
SELECT
  pip_in_tile.pkey,
  pip_in_tile.src_wire_in_tile_pkey,
  pip_in_tile.dest_wire_in_tile_pkey,
  wire_in_node.wire_pkey,
  wire_in_node.wire_in_tile_pkey,
  wire_in_node.phy_tile_pkey
FROM
  wire_in_node
  INNER JOIN pip_in_tile
WHERE
  pip_in_tile.is_pseudo = 0 AND (
  pip_in_tile.src_wire_in_tile_pkey = wire_in_node.wire_in_tile_pkey
  OR pip_in_tile.dest_wire_in_tile_pkey = wire_in_node.wire_in_tile_pkey)
LIMIT
  1;
""", (node, )
            )

            (
                pip_pkey, src_wire_in_tile_pkey, dest_wire_in_tile_pkey,
                wire_in_node_pkey, wire_in_tile_pkey, phy_tile_pkey
            ) = write_cur.fetchone()
            assert write_cur.fetchone() is None, node

            assert (
                wire_in_tile_pkey == src_wire_in_tile_pkey
                or wire_in_tile_pkey == dest_wire_in_tile_pkey
            ), (wire_in_tile_pkey, pip_pkey)

            if src_wire_in_tile_pkey == wire_in_tile_pkey:
                other_wire = dest_wire_in_tile_pkey
            else:
                other_wire = src_wire_in_tile_pkey

            write_cur.execute(
                """
            SELECT node_pkey FROM wire WHERE
                wire_in_tile_pkey = ? AND
                phy_tile_pkey = ?;
                """, (other_wire, phy_tile_pkey)
            )

            (other_node_pkey, ) = write_cur.fetchone()
            assert write_cur.fetchone() is None
            assert other_node_pkey is not None, (other_wire, phy_tile_pkey)

            write_cur.execute(
                """
            SELECT site_wire_pkey, number_pips
                FROM node WHERE pkey = ?;
                """, (other_node_pkey, )
            )

            result = write_cur.fetchone()
            assert result is not None, other_node_pkey
            other_site_wire_pkey, other_number_pips = result
            assert write_cur.fetchone() is None

            if other_site_wire_pkey is not None and other_number_pips == 1:
                if src_wire_in_tile_pkey == wire_in_tile_pkey:
                    src_wire_pkey = site_wire_pkey
                    dest_wire_pkey = other_site_wire_pkey
                else:
                    src_wire_pkey = other_site_wire_pkey
                    dest_wire_pkey = site_wire_pkey

                edge_with_mux.append(
                    (
                        (node, other_node_pkey), src_wire_pkey, dest_wire_pkey,
                        pip_pkey
                    )
                )
            elif other_site_wire_pkey is None and other_number_pips == 1:
                null_nodes.append(node)
                null_nodes.append(other_node_pkey)
                pass
            else:
                edges_to_channel.append(node)

    for nodes, src_wire_pkey, dest_wire_pkey, pip_pkey in progressbar.progressbar(
            edge_with_mux):
        assert len(nodes) == 2

        switch_pkey = check_edge_with_mux_timing(
            conn, get_switch_timing, src_wire_pkey, dest_wire_pkey, pip_pkey
        )
        write_cur.execute(
            """
        UPDATE node SET classification = ?
            WHERE pkey IN (?, ?);""",
            (NodeClassification.EDGE_WITH_MUX.value, nodes[0], nodes[1])
        )

        write_cur.execute(
            """
INSERT INTO edge_with_mux(src_wire_pkey, dest_wire_pkey, pip_in_tile_pkey, switch_pkey)
VALUES
  (?, ?, ?, ?);""", (src_wire_pkey, dest_wire_pkey, pip_pkey, switch_pkey)
        )

    for node in progressbar.progressbar(edges_to_channel):
        write_cur.execute(
            """
        UPDATE node SET classification = ?
            WHERE pkey = ?;""", (
                NodeClassification.EDGES_TO_CHANNEL.value,
                node,
            )
        )

    for null_node in progressbar.progressbar(null_nodes):
        write_cur.execute(
            """
        UPDATE node SET classification = ?
            WHERE pkey = ?;""", (
                NodeClassification.NULL.value,
                null_node,
            )
        )

    write_cur.execute("CREATE INDEX node_type_index ON node(classification);")
    write_cur.connection.commit()


def get_node_rc(conn, node_pkey):
    """ Returns capacitance and resistance for given node.

    Returns
    -------
    capacitance : float
        Node capacitance (Farads)
    resistance : float
        Node resistance (Ohms)

    """
    capacitance = 0
    resistance = 0

    cur = conn.cursor()
    for wire_cap, wire_res in cur.execute("""
SELECT capacitance, resistance FROM wire_in_tile WHERE pkey IN (
    SELECT wire_in_tile_pkey FROM wire WHERE node_pkey = ?
    );""", (node_pkey, )):
        capacitance += wire_cap
        resistance += wire_res

    return capacitance, resistance


def insert_tracks(conn, tracks_to_insert):
    write_cur = conn.cursor()
    write_cur.execute('SELECT pkey FROM switch WHERE name = "short";')
    short_pkey = write_cur.fetchone()[0]

    track_graph_nodes = {}
    track_pkeys = []
    for node, tracks_list, track_connections, tracks_model in progressbar.progressbar(
            tracks_to_insert):
        write_cur.execute("""INSERT INTO track DEFAULT VALUES""")
        track_pkey = write_cur.lastrowid
        track_pkeys.append(track_pkey)

        write_cur.execute(
            """UPDATE node SET track_pkey = ? WHERE pkey = ?""",
            (track_pkey, node)
        )

        track_graph_node_pkey = []
        for idx, track in enumerate(tracks_list):
            if track.direction == 'X':
                node_type = graph2.NodeType.CHANX
            elif track.direction == 'Y':
                node_type = graph2.NodeType.CHANY
            else:
                assert False, track.direction

            if idx == 0:
                capacitance, resistance = get_node_rc(conn, node)
            else:
                capacitance = 0
                resistance = 0

            write_cur.execute(
                """
INSERT INTO graph_node(
  graph_node_type, track_pkey, node_pkey,
  x_low, x_high, y_low, y_high, capacity, capacitance, resistance
)
VALUES
  (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""", (
                    node_type.value, track_pkey, node, track.x_low,
                    track.x_high, track.y_low, track.y_high, capacitance,
                    resistance
                )
            )
            track_graph_node_pkey.append(write_cur.lastrowid)

        track_graph_nodes[node] = track_graph_node_pkey

        for connection in track_connections:
            write_cur.execute(
                """
INSERT INTO graph_edge(
  src_graph_node_pkey, dest_graph_node_pkey,
  switch_pkey, track_pkey
)
VALUES
  (?, ?, ?, ?),
  (?, ?, ?, ?)""", (
                    track_graph_node_pkey[connection[0]],
                    track_graph_node_pkey[connection[1]],
                    short_pkey,
                    track_pkey,
                    track_graph_node_pkey[connection[1]],
                    track_graph_node_pkey[connection[0]],
                    short_pkey,
                    track_pkey,
                )
            )

    conn.commit()

    wire_to_graph = {}
    for node, tracks_list, track_connections, tracks_model in progressbar.progressbar(
            tracks_to_insert):
        track_graph_node_pkey = track_graph_nodes[node]

        write_cur.execute(
            """
WITH wires_from_node(wire_pkey, tile_pkey) AS (
  SELECT
    pkey,
    tile_pkey
  FROM
    wire
  WHERE
    node_pkey = ?
)
SELECT
  wires_from_node.wire_pkey,
  tile.grid_x,
  tile.grid_y
FROM
  tile
  INNER JOIN wires_from_node ON tile.pkey = wires_from_node.tile_pkey;
  """, (node, )
        )

        wires = write_cur.fetchall()

        for wire_pkey, grid_x, grid_y in wires:
            connections = list(
                tracks_model.get_tracks_for_wire_at_coord((grid_x, grid_y))
            )
            assert len(connections) > 0, (
                wire_pkey, track_pkey, grid_x, grid_y
            )
            graph_node_pkey = track_graph_node_pkey[connections[0][0]]

            wire_to_graph[wire_pkey] = graph_node_pkey

    for wire_pkey, graph_node_pkey in progressbar.progressbar(
            wire_to_graph.items()):
        write_cur.execute(
            """
        UPDATE wire SET graph_node_pkey = ?
            WHERE pkey = ?""", (graph_node_pkey, wire_pkey)
        )

    conn.commit()

    write_cur.execute(
        """CREATE INDEX graph_node_nodes ON graph_node(node_pkey);"""
    )
    write_cur.execute(
        """CREATE INDEX graph_node_tracks ON graph_node(track_pkey);"""
    )
    write_cur.execute(
        """CREATE INDEX graph_edge_tracks ON graph_edge(track_pkey);"""
    )

    conn.commit()
    return track_pkeys


def create_track(node, unique_pos):
    xs, ys = points.decompose_points_into_tracks(unique_pos)
    tracks_list, track_connections = tracks.make_tracks(xs, ys, unique_pos)
    tracks_model = tracks.Tracks(tracks_list, track_connections)

    return [node, tracks_list, track_connections, tracks_model]


def form_tracks(conn):
    cur = conn.cursor()

    cur.execute(
        'SELECT count(pkey) FROM node WHERE classification == ?;',
        (NodeClassification.CHANNEL.value, )
    )
    num_nodes = cur.fetchone()[0]

    tracks_to_insert = []
    with progressbar.ProgressBar(max_value=num_nodes) as bar:
        bar.update(0)
        cur2 = conn.cursor()
        for idx, (node, ) in enumerate(cur.execute("""
SELECT pkey FROM node WHERE classification == ?;
""", (NodeClassification.CHANNEL.value, ))):
            bar.update(idx)

            unique_pos = set()
            for wire_pkey, grid_x, grid_y in cur2.execute("""
WITH wires_from_node(wire_pkey, tile_pkey) AS (
  SELECT
    pkey,
    tile_pkey
  FROM
    wire
  WHERE
    node_pkey = ? AND tile_pkey IS NOT NULL
)
SELECT
  wires_from_node.wire_pkey,
  tile.grid_x,
  tile.grid_y
FROM
  tile
  INNER JOIN wires_from_node ON tile.pkey = wires_from_node.tile_pkey;
  """, (node, )):
                unique_pos.add((grid_x, grid_y))

            tracks_to_insert.append(create_track(node, unique_pos))

    # Create constant tracks
    vcc_track_to_insert, gnd_track_to_insert = create_constant_tracks(conn)
    vcc_idx = len(tracks_to_insert)
    tracks_to_insert.append(vcc_track_to_insert)
    gnd_idx = len(tracks_to_insert)
    tracks_to_insert.append(gnd_track_to_insert)

    track_pkeys = insert_tracks(conn, tracks_to_insert)
    vcc_track_pkey = track_pkeys[vcc_idx]
    gnd_track_pkey = track_pkeys[gnd_idx]

    write_cur = conn.cursor()
    write_cur.execute(
        """
INSERT INTO constant_sources(vcc_track_pkey, gnd_track_pkey) VALUES (?, ?)
        """, (
            vcc_track_pkey,
            gnd_track_pkey,
        )
    )

    conn.commit()

    connect_hardpins_to_constant_network(conn, vcc_track_pkey, gnd_track_pkey)


def connect_hardpins_to_constant_network(conn, vcc_track_pkey, gnd_track_pkey):
    """ Connect TIEOFF HARD1 and HARD0 pins.

    Update nodes connected to to HARD1 or HARD0 pins to point to the new
    VCC or GND track.  This should connect the pips to the constant
    network instead of the TIEOFF site.
    """

    cur = conn.cursor()
    cur.execute(
        """
SELECT pkey FROM site_type WHERE name = ?
""", ("TIEOFF", )
    )
    results = cur.fetchall()
    assert len(results) == 1, results
    tieoff_site_type_pkey = results[0][0]

    cur.execute(
        """
SELECT pkey FROM site_pin WHERE site_type_pkey = ? and name = ?
""", (tieoff_site_type_pkey, "HARD1")
    )
    vcc_site_pin_pkey = cur.fetchone()[0]
    cur.execute(
        """
SELECT pkey FROM wire_in_tile WHERE site_pin_pkey = ?
""", (vcc_site_pin_pkey, )
    )

    cur.execute(
        """
SELECT pkey FROM wire_in_tile WHERE site_pin_pkey = ?
""", (vcc_site_pin_pkey, )
    )

    write_cur = conn.cursor()
    write_cur.execute("""BEGIN EXCLUSIVE TRANSACTION;""")

    for (wire_in_tile_pkey, ) in cur:
        write_cur.execute(
            """
UPDATE node SET track_pkey = ? WHERE pkey IN (
    SELECT node_pkey FROM wire WHERE wire_in_tile_pkey = ?
)
            """, (
                vcc_track_pkey,
                wire_in_tile_pkey,
            )
        )

    cur.execute(
        """
SELECT pkey FROM site_pin WHERE site_type_pkey = ? and name = ?
""", (tieoff_site_type_pkey, "HARD0")
    )
    gnd_site_pin_pkey = cur.fetchone()[0]

    cur.execute(
        """
SELECT pkey FROM wire_in_tile WHERE site_pin_pkey = ?
""", (gnd_site_pin_pkey, )
    )
    for (wire_in_tile_pkey, ) in cur:
        write_cur.execute(
            """
UPDATE node SET track_pkey = ? WHERE pkey IN (
    SELECT node_pkey FROM wire WHERE wire_in_tile_pkey = ?
)
            """, (
                gnd_track_pkey,
                wire_in_tile_pkey,
            )
        )

    write_cur.execute("""COMMIT TRANSACTION""")


def traverse_pip(conn, wire_in_tile_pkey):
    """ Given a generic wire, find (if any) the wire on the other side of a pip.

    Returns None if no wire or pip connects to this wire.

    """
    cur = conn.cursor()

    cur.execute(
        """
SELECT src_wire_in_tile_pkey FROM pip_in_tile WHERE
    is_pseudo = 0 AND
    dest_wire_in_tile_pkey = ?
    ;""", (wire_in_tile_pkey, )
    )

    result = cur.fetchone()
    if result is not None:
        return result[0]

    cur.execute(
        """
SELECT dest_wire_in_tile_pkey FROM pip_in_tile WHERE
    is_pseudo = 0 AND
    src_wire_in_tile_pkey = ?
    ;""", (wire_in_tile_pkey, )
    )

    result = cur.fetchone()
    if result is not None:
        return result[0]

    return None


def create_vpr_grid(conn):
    """ Create VPR grid from prjxray grid. """
    cur = conn.cursor()
    cur2 = conn.cursor()

    write_cur = conn.cursor()
    write_cur.execute("""BEGIN EXCLUSIVE TRANSACTION;""")

    # Insert synthetic tile types for CLB sites.
    write_cur.execute('INSERT INTO tile_type(name) VALUES ("SLICEL");')
    slicel_tile_type_pkey = write_cur.lastrowid

    write_cur.execute('INSERT INTO tile_type(name) VALUES ("SLICEM");')
    slicem_tile_type_pkey = write_cur.lastrowid

    slice_types = {
        'SLICEL': slicel_tile_type_pkey,
        'SLICEM': slicem_tile_type_pkey,
    }

    tiles_to_split = {
        'CLBLL_L': tile_splitter.grid.WEST,
        'CLBLL_R': tile_splitter.grid.EAST,
        'CLBLM_L': tile_splitter.grid.WEST,
        'CLBLM_R': tile_splitter.grid.EAST,
    }

    # Create initial grid using sites and locations from phy_tile's
    # Also build up tile_to_tile_type_pkeys, which is a map from original
    # tile_type_pkey, to array of split tile type pkeys, (e.g. SLICEL/SLICEM).
    tile_to_tile_type_pkeys = {}
    grid_loc_map = {}
    for phy_tile_pkey, tile_type_pkey, grid_x, grid_y in progressbar.progressbar(
            cur.execute("""
        SELECT pkey, tile_type_pkey, grid_x, grid_y FROM phy_tile;
        """)):

        cur2.execute(
            "SELECT name FROM tile_type WHERE pkey = ?;", (tile_type_pkey, )
        )
        tile_type_name = cur2.fetchone()[0]

        sites = []
        site_pkeys = set()
        for (site_pkey, ) in cur2.execute("""
            SELECT site_pkey FROM wire_in_tile WHERE tile_type_pkey = ? AND site_pkey IS NOT NULL;""",
                                          (tile_type_pkey, )):
            site_pkeys.add(site_pkey)

        for site_pkey in site_pkeys:
            cur2.execute(
                """
                SELECT x_coord, y_coord, site_type_pkey
                FROM site WHERE pkey = ?;""", (site_pkey, )
            )
            result = cur2.fetchone()
            assert result is not None, (tile_type_pkey, site_pkey)
            x, y, site_type_pkey = result

            cur2.execute(
                "SELECT name FROM site_type WHERE pkey = ?;",
                ((site_type_pkey, ))
            )
            site_type_name = cur2.fetchone()[0]

            sites.append(
                tile_splitter.grid.Site(
                    name=site_type_name,
                    phy_tile_pkey=phy_tile_pkey,
                    tile_type_pkey=tile_type_pkey,
                    site_type_pkey=site_type_pkey,
                    site_pkey=site_pkey,
                    x=x,
                    y=y
                )
            )

        sites = sorted(sites, key=lambda s: (s.x, s.y))

        if tile_type_name in tiles_to_split:
            tile_type_pkeys = []
            for site in sites:
                tile_type_pkeys.append(slice_types[site.name])

            if tile_type_name in tile_to_tile_type_pkeys:
                assert tile_to_tile_type_pkeys[tile_type_name] == \
                        tile_type_pkeys, (tile_type_name,)
            else:
                tile_to_tile_type_pkeys[tile_type_name] = tile_type_pkeys

        grid_loc_map[(grid_x, grid_y)] = tile_splitter.grid.Tile(
            root_phy_tile_pkeys=[phy_tile_pkey],
            phy_tile_pkeys=[phy_tile_pkey],
            tile_type_pkey=tile_type_pkey,
            sites=sites
        )

    cur.execute('SELECT pkey FROM tile_type WHERE name = "NULL";')
    empty_tile_type_pkey = cur.fetchone()[0]

    tile_types = {}
    for tile_type, split_direction in tiles_to_split.items():
        cur.execute(
            'SELECT pkey FROM tile_type WHERE name = ?;', (tile_type, )
        )
        tile_type_pkey = cur.fetchone()[0]
        tile_types[tile_type] = tile_type_pkey

    vpr_grid = tile_splitter.grid.Grid(
        grid_loc_map=grid_loc_map, empty_tile_type_pkey=empty_tile_type_pkey
    )

    for tile_type, split_direction in tiles_to_split.items():
        vpr_grid.split_tile_type(
            tile_type_pkey=tile_types[tile_type],
            tile_type_pkeys=tile_to_tile_type_pkeys[tile_type],
            split_direction=split_direction
        )

    new_grid = vpr_grid.output_grid()
    # Create tile rows for each tile in the VPR grid.  As provide map entries
    # to physical grid and alias map from split tile type to original tile
    # type.
    for (grid_x, grid_y), tile in new_grid.items():
        # TODO: Merging of tiles isn't supported yet, so don't handle multiple
        # phy_tile_pkeys yet. The phy_tile_pkey to add to the new VPR tile
        # should be the tile to use on the FASM prefix.
        assert len(tile.phy_tile_pkeys) == 1
        assert len(tile.root_phy_tile_pkeys) in [0, 1], len(
            tile.root_phy_tile_pkeys
        )

        if tile.split_sites:
            assert len(tile.sites) == 1
            write_cur.execute(
                """
SELECT pkey, parent_tile_type_pkey FROM site_as_tile WHERE tile_type_pkey = ? AND site_pkey = ?""",
                (tile.sites[0].tile_type_pkey, tile.sites[0].site_pkey)
            )
            result = write_cur.fetchone()

            if result is None:
                write_cur.execute(
                    """
INSERT INTO
    site_as_tile(parent_tile_type_pkey, tile_type_pkey, site_pkey)
VALUES
    (?, ?, ?);""", (
                        tile.tile_type_pkey, tile.sites[0].tile_type_pkey,
                        tile.sites[0].site_pkey
                    )
                )
                site_as_tile_pkey = write_cur.lastrowid
            else:
                site_as_tile_pkey, parent_tile_type_pkey = result
                assert parent_tile_type_pkey == tile.tile_type_pkey

            # Mark that this tile is split by setting the site_as_tile_pkey.
            write_cur.execute(
                """
INSERT INTO tile(phy_tile_pkey, tile_type_pkey, site_as_tile_pkey, grid_x, grid_y) VALUES (
        ?, ?, ?, ?, ?)""", (
                    tile.phy_tile_pkeys[0], tile.tile_type_pkey,
                    site_as_tile_pkey, grid_x, grid_y
                )
            )
        else:
            write_cur.execute(
                """
INSERT INTO tile(phy_tile_pkey, tile_type_pkey, grid_x, grid_y) VALUES (
        ?, ?, ?, ?)""",
                (tile.phy_tile_pkeys[0], tile.tile_type_pkey, grid_x, grid_y)
            )

        tile_pkey = write_cur.lastrowid

        # Build the phy_tile <-> tile map.
        for phy_tile_pkey in tile.phy_tile_pkeys:
            write_cur.execute(
                """
INSERT INTO tile_map(tile_pkey, phy_tile_pkey) VALUES (?, ?)
                """, (tile_pkey, phy_tile_pkey)
            )

        # First assign all wires at the root_phy_tile_pkeys to this tile_pkey.
        # This ensures all wires, (including wires without sites) have a home.
        for root_phy_tile_pkey in tile.root_phy_tile_pkeys:
            write_cur.execute(
                """
UPDATE
    wire
SET
    tile_pkey = ?
WHERE
    phy_tile_pkey = ?
    ;""", (tile_pkey, root_phy_tile_pkey)
            )

    write_cur.execute(
        "CREATE INDEX tile_location_index ON tile(grid_x, grid_y);"
    )
    write_cur.execute("CREATE INDEX tile_to_phy_map ON tile_map(tile_pkey);")
    write_cur.execute(
        "CREATE INDEX phy_to_tile_map ON tile_map(phy_tile_pkey);"
    )
    write_cur.execute("""COMMIT TRANSACTION;""")

    write_cur.execute("""BEGIN EXCLUSIVE TRANSACTION;""")

    # At this point all wires have a tile_pkey that corrisponds to the old root
    # tile.  Wires belonging to sites need to be reassigned to their respective
    # tiles.
    for (grid_x, grid_y), tile in new_grid.items():
        cur2.execute(
            "SELECT pkey FROM tile WHERE grid_x = ? AND grid_y = ?;",
            (grid_x, grid_y)
        )
        tile_pkey = cur2.fetchone()[0]

        for site in tile.sites:
            cur2.execute(
                "SELECT tile_type_pkey FROM phy_tile WHERE pkey = ?;",
                (site.phy_tile_pkey, )
            )
            tile_type_pkey = cur2.fetchone()[0]

            # Find all wires that belong to the new tile location.
            for wire_pkey, wire_in_tile_pkey in cur2.execute("""
WITH wires(wire_pkey, wire_in_tile_pkey) AS (
    SELECT
        pkey, wire_in_tile_pkey
    FROM
        wire
    WHERE
        phy_tile_pkey = ?
)
SELECT
    wires.wire_pkey, wires.wire_in_tile_pkey
FROM
    wires
INNER JOIN
    wire_in_tile
ON
    wire_in_tile.pkey = wires.wire_in_tile_pkey
WHERE
    wire_in_tile.site_pkey = ?
    ;""", (site.phy_tile_pkey, site.site_pkey)):
                # Move the wire to the new tile_pkey.
                write_cur.execute(
                    """
UPDATE
    wire
SET
    tile_pkey = ?
WHERE
    pkey = ?;""", (
                        tile_pkey,
                        wire_pkey,
                    )
                )

                # Wires connected to the site via a pip require traversing the
                # pip.
                other_wire_in_tile_pkey = traverse_pip(conn, wire_in_tile_pkey)
                if other_wire_in_tile_pkey is not None:
                    # A wire was found connected to the site via pip, reassign
                    # tile_pkey.
                    write_cur.execute(
                        """
UPDATE
    wire
SET
    tile_pkey = ?
WHERE
    phy_tile_pkey = ?
AND
    wire_in_tile_pkey = ?
    ;""", (tile_pkey, site.phy_tile_pkey, other_wire_in_tile_pkey)
                    )

    # Now that final wire <-> tile assignments are made, create the index.
    write_cur.execute(
        "CREATE INDEX tile_wire_index ON wire(wire_in_tile_pkey, tile_pkey);"
    )
    write_cur.execute("""COMMIT TRANSACTION;""")


def create_constant_tracks(conn):
    """ Create two tracks that go to all TIEOFF sites to route constants.

    Returns (vcc_track_to_insert, gnd_track_to_insert), suitable for insert
    via insert_tracks function.

    """

    # Make constant track available to all tiles.
    write_cur = conn.cursor()
    unique_pos = set()
    write_cur.execute('SELECT grid_x, grid_y FROM tile')
    for grid_x, grid_y in write_cur:
        if grid_x == 0 or grid_y == 0:
            continue
        unique_pos.add((grid_x, grid_y))

    write_cur.execute(
        """
INSERT INTO node(classification) VALUES (?)
""", (NodeClassification.CHANNEL.value, )
    )
    vcc_node = write_cur.lastrowid

    write_cur.execute(
        """
INSERT INTO node(classification) VALUES (?)
""", (NodeClassification.CHANNEL.value, )
    )
    gnd_node = write_cur.lastrowid

    conn.commit()

    return create_track(vcc_node, unique_pos), \
           create_track(gnd_node, unique_pos)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--db_root', help='Project X-Ray Database', required=True
    )
    parser.add_argument(
        '--connection_database', help='Connection database', required=True
    )

    args = parser.parse_args()
    if os.path.exists(args.connection_database):
        os.remove(args.connection_database)

    with DatabaseCache(args.connection_database) as conn:

        create_tables(conn)

        print("{}: About to load database".format(datetime.datetime.now()))
        db = prjxray.db.Database(args.db_root)
        grid = db.grid()
        get_switch, get_switch_timing = create_get_switch(conn)
        import_phy_grid(db, grid, conn, get_switch, get_switch_timing)
        print("{}: Initial database formed".format(datetime.datetime.now()))
        import_nodes(db, grid, conn)
        print("{}: Connections made".format(datetime.datetime.now()))
        count_sites_and_pips_on_nodes(conn)
        print("{}: Counted sites and pips".format(datetime.datetime.now()))
        classify_nodes(conn, get_switch_timing)
        print("{}: Create VPR grid".format(datetime.datetime.now()))
        create_vpr_grid(conn)
        print("{}: Nodes classified".format(datetime.datetime.now()))
        form_tracks(conn)
        print("{}: Tracks formed".format(datetime.datetime.now()))

        print(
            '{} Flushing database back to file "{}"'.format(
                datetime.datetime.now(), args.connection_database
            )
        )


if __name__ == '__main__':
    main()
