/*
 * `output wire rdclk` should be detected as a clock despite this being a black
 * box module.
 */
(* whitebox *)
module block(a, b, rdclk);
	input wire a;
	input wire b;
	output wire rdclk;
endmodule
